"""The one place the lab turns a ``LabDataset`` + window into a backtest.

Objectives and survival tests all go through here so the dataset's
interval, universe and starting cash can't drift between call sites
(previously several of them silently backtested intraday datasets at 1d).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.report import BacktestReport
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.interval import Interval
from stonks.core.protocols import Strategy
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
    """Backtest ``strategy`` on ``dataset`` over ``window``.

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
    report = Backtester(
        strategies=[strategy],
        broker=broker,
        lake=lake if lake is not None else dataset.lake,
        config=backtest_config(dataset, window),
    ).run()
    return report, broker.reconcile()
