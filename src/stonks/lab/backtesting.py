"""The one place the lab turns a ``LabDataset`` + window into a backtest.

Objectives and survival tests all go through here so the dataset's
interval, universe and starting cash can't drift between call sites
(previously several of them silently backtested intraday datasets at 1d).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pandas as pd

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.report import BacktestReport
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.backtest.trades import with_trades
from stonks.core.interval import Interval
from stonks.core.protocols import Strategy
from stonks.core.timeutil import day_end, day_start
from stonks.core.types import Fill, Portfolio

#: Starting cash for every lab backtest. Scores are scale-free
#: (Sharpe, returns), so the value only needs to be consistent.
LAB_INITIAL_CASH = 10_000.0


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
    )


def run_backtest(
    strategy: Strategy,
    dataset: Any,
    window: tuple[date | datetime, date | datetime],
    lake: Any = None,
) -> BacktestReport:
    """Backtest ``strategy`` on ``dataset`` over ``window``, with the
    round-trip trade ledger attached (``report.trades``/``trade_stats``).

    ``lake`` overrides ``dataset.lake`` — survival tests that build a
    modified copy of the bars (permuted, perturbed) pass it here.
    """
    report, _ = run_backtest_with_fills(strategy, dataset, window, lake)
    return report


def run_backtest_with_fills(
    strategy: Strategy,
    dataset: Any,
    window: tuple[date | datetime, date | datetime],
    lake: Any = None,
) -> tuple[BacktestReport, list[Fill]]:
    """:func:`run_backtest` plus every fill the simulated broker made, in
    fill order — for tests that look at trades rather than the equity
    curve (e.g. the trade-level runs test)."""
    costs = getattr(dataset, "costs", None)
    broker = SimulatedBroker(
        portfolio=Portfolio(cash=LAB_INITIAL_CASH, positions={}),
        cost_model=costs.build() if costs is not None else None,
    )
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
    return report, list(fills)


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
