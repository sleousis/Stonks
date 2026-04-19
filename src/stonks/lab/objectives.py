"""Objective implementations — strategy-agnostic scoring functions.

Each objective wraps a quick backtest on the training window and extracts a
single scalar the tuner maximizes or minimizes.
"""

from __future__ import annotations

from typing import Literal

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.report import BacktestReport
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.protocols import Strategy
from stonks.core.types import Portfolio
from stonks.lab.dataset import LabDataset


def _backtest(strategy: Strategy, dataset: LabDataset, window: tuple) -> BacktestReport:
    start, end = window
    broker = SimulatedBroker(portfolio=Portfolio(cash=10_000.0, positions={}))
    bt = Backtester(
        strategies=[strategy],
        broker=broker,
        lake=dataset.lake,
        config=BacktestConfig(
            start=start,
            end=end,
            universe=list(dataset.universe),
            threshold=0.0,
        ),
    )
    return bt.run()


class SharpeObjective:
    name = "sharpe"
    direction: Literal["maximize", "minimize"] = "maximize"

    def score(self, strategy: Strategy, dataset: LabDataset) -> float:
        return _backtest(strategy, dataset, dataset.train_window).sharpe


class CAGRObjective:
    name = "cagr"
    direction: Literal["maximize", "minimize"] = "maximize"

    def score(self, strategy: Strategy, dataset: LabDataset) -> float:
        return _backtest(strategy, dataset, dataset.train_window).cagr


class FinalReturnObjective:
    name = "final_return"
    direction: Literal["maximize", "minimize"] = "maximize"

    def score(self, strategy: Strategy, dataset: LabDataset) -> float:
        return _backtest(strategy, dataset, dataset.train_window).final_return
