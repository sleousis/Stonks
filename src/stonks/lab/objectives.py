"""Objective implementations — strategy-agnostic scoring functions.

Each objective wraps a quick backtest on the training window and extracts a
single scalar the tuner maximizes or minimizes.

``evaluate(strategy, dataset)`` returns the whole :class:`TrialOutcome`:
the score plus the per-bar returns of the equity curve behind it (what the
trial ledger stores). ``score`` is ``evaluate(...).score``. Objectives
without ``evaluate`` still work with the tuners (see
``lab.tuning.base.evaluate_trial``); they just record no returns.
"""

from __future__ import annotations

from typing import Literal

import numpy as np

from stonks.backtest.report import BacktestReport
from stonks.core.protocols import Strategy, TrialOutcome
from stonks.lab.backtesting import run_backtest as _backtest
from stonks.lab.dataset import LabDataset


class _BacktestObjective:
    name: str
    direction: Literal["maximize", "minimize"] = "maximize"

    def metric(self, report: BacktestReport) -> float:  # pragma: no cover - abstract
        raise NotImplementedError

    def evaluate(self, strategy: Strategy, dataset: LabDataset) -> TrialOutcome:
        report = _backtest(strategy, dataset, dataset.train_window)
        returns, index = per_bar_returns(report)
        return TrialOutcome(
            params=dict(getattr(strategy, "params", None) or {}),
            score=float(self.metric(report)),
            returns=returns,
            index=index,
        )

    def score(self, strategy: Strategy, dataset: LabDataset) -> float:
        return self.evaluate(strategy, dataset).score


def per_bar_returns(report: BacktestReport) -> tuple[np.ndarray, np.ndarray]:
    """Simple returns of ``report``'s equity curve, one per bar after the
    first, with those bars' timestamps (``datetime64[ns]``). A bar after a
    zero equity value has a NaN return."""
    curve = np.asarray(report.equity_curve, dtype=np.float64)
    index = np.array(report.equity_dates[1:], dtype="datetime64[ns]")
    if len(curve) < 2:
        return np.empty(0, dtype=np.float64), index[:0]
    prev = curve[:-1]
    returns = np.full(len(prev), np.nan)
    np.divide(curve[1:], prev, out=returns, where=prev != 0)
    return returns - 1.0, index


class SharpeObjective(_BacktestObjective):
    name = "sharpe"
    direction: Literal["maximize", "minimize"] = "maximize"

    def metric(self, report: BacktestReport) -> float:
        return report.sharpe


class CAGRObjective(_BacktestObjective):
    name = "cagr"
    direction: Literal["maximize", "minimize"] = "maximize"

    def metric(self, report: BacktestReport) -> float:
        return report.cagr


class FinalReturnObjective(_BacktestObjective):
    name = "final_return"
    direction: Literal["maximize", "minimize"] = "maximize"

    def metric(self, report: BacktestReport) -> float:
        return report.final_return
