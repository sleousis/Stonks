"""The one place the lab turns a ``LabDataset`` + window into a backtest.

Objectives and survival tests all go through here so the dataset's
interval, universe and starting cash can't drift between call sites
(previously several of them silently backtested intraday datasets at 1d).

Every report carries its benchmark (BL-22): a ``BenchmarkedReport`` whose
``benchmark`` is computed by ``stonks.backtest.benchmark`` on the same bars
and timestamps, from ``getattr(dataset, "benchmark", "auto")`` unless the
caller passes ``benchmark=`` (``"none"`` turns it off).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pandas as pd

from stonks.backtest.benchmark import (
    DEFAULT_BENCHMARK,
    BenchmarkedReport,
    benchmark_curve,
    with_benchmark,
)
from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.fills import ExecutionSettings
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.backtest.trades import with_trades
from stonks.core.interval import Interval
from stonks.core.protocols import Strategy
from stonks.core.timeutil import day_end, day_start
from stonks.core.types import Fill, Portfolio

#: Starting cash for every lab backtest. Scores are scale-free
#: (Sharpe, returns), so the value only needs to be consistent.
LAB_INITIAL_CASH = 10_000.0

#: Default for ``benchmark=``: read the dataset's ``benchmark`` attribute.
FROM_DATASET = "__from_dataset__"


def backtest_config(
    dataset: Any, window: tuple[date | datetime, date | datetime]
) -> BacktestConfig:
    """Build a ``BacktestConfig`` for ``dataset`` over ``window``."""
    start, end = window
    return BacktestConfig(
        start=start,
        end=end,
        universe=list(dataset.universe),
        interval=getattr(dataset, "interval", Interval.DAY_1),
        threshold=0.0,
        construction=getattr(dataset, "construction", None),
        universe_id=getattr(dataset, "universe_id", None),
    )


def lab_broker(dataset: Any) -> SimulatedBroker:
    """The simulated broker of every lab backtest on ``dataset``: its cost
    model and execution settings (fill model, settlement)."""
    costs = getattr(dataset, "costs", None)
    execution = getattr(dataset, "execution", None) or ExecutionSettings()
    return SimulatedBroker.from_execution(
        Portfolio(cash=LAB_INITIAL_CASH, positions={}),
        execution,
        cost_model=costs.build() if costs is not None else None,
    )


def run_backtest(
    strategy: Strategy,
    dataset: Any,
    window: tuple[date | datetime, date | datetime],
    lake: Any = None,
    *,
    benchmark: str | None = FROM_DATASET,
) -> BenchmarkedReport:
    """Backtest ``strategy`` on ``dataset`` over ``window``, with the
    round-trip trade ledger (``report.trades``/``trade_stats``) and the
    benchmark (``report.benchmark``; ``None`` when off or unpriced) attached.

    ``lake`` overrides ``dataset.lake`` — survival tests that build a
    modified copy of the bars (permuted, perturbed) pass it here; the
    benchmark then comes from the same modified bars.
    """
    report, _ = run_backtest_with_fills(strategy, dataset, window, lake, benchmark=benchmark)
    return report


def run_backtest_with_fills(
    strategy: Strategy,
    dataset: Any,
    window: tuple[date | datetime, date | datetime],
    lake: Any = None,
    *,
    benchmark: str | None = FROM_DATASET,
) -> tuple[BenchmarkedReport, list[Fill]]:
    """:func:`run_backtest` plus every fill the simulated broker made, in
    fill order — for tests that look at trades rather than the equity
    curve (e.g. the trade-level runs test)."""
    broker = lab_broker(dataset)
    lake = lake if lake is not None else dataset.lake
    config = backtest_config(dataset, window)
    report = Backtester(strategies=[strategy], broker=broker, lake=lake, config=config).run()
    fills = broker.fills
    report = with_trades(
        report,
        fills,
        _trade_bars(lake, config, sorted({f.ticker for f in fills})),
        reference_price=broker.reference_price,
    )
    spec = (
        getattr(dataset, "benchmark", DEFAULT_BENCHMARK) if benchmark == FROM_DATASET else benchmark
    )
    curve = benchmark_curve(
        lake,
        spec,
        report.equity_dates,
        universe=config.universe,
        interval=config.interval,
        initial_value=report.equity_curve[0] if report.equity_curve else 1.0,
    )
    return with_benchmark(report, curve), list(fills)


def _trade_bars(lake: Any, config: BacktestConfig, tickers: list[str]) -> pd.DataFrame | None:
    """High/low/close of the traded tickers over the backtest window, for
    MAE/MFE and open-lot marks; one query, skipped when nothing traded."""
    if not tickers:
        return None
    return lake.sql(
        """
        SELECT ticker, timestamp, high, low, close
          FROM bars
         WHERE ticker = ANY(?) AND interval = ? AND timestamp BETWEEN ? AND ?
         ORDER BY ticker, timestamp
        """,
        [tickers, config.interval.code, day_start(config.start), day_end(config.end)],
    )
