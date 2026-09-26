"""Objective implementations — strategy-agnostic scoring functions.

Each objective wraps a quick backtest on the training window and extracts a
single scalar the tuner maximizes or minimizes.
"""

from __future__ import annotations

from typing import Literal

from stonks.core.protocols import Strategy
from stonks.lab.backtesting import run_backtest as _backtest
from stonks.lab.dataset import LabDataset


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
