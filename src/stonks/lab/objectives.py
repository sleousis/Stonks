"""Objective implementations — strategy-agnostic scoring functions.

Each objective wraps a quick backtest on the training window and extracts a
single scalar the tuner maximizes or minimizes.

``evaluate(strategy, dataset)`` returns the whole :class:`TrialOutcome`:
the score plus the per-bar returns of the equity curve behind it (what the
trial ledger stores). ``score`` is ``evaluate(...).score``. Objectives
without ``evaluate`` still work with the tuners (see
``lab.tuning.base.evaluate_trial``); they just record no returns.

Risk-aware objectives (22.1): ``sortino``, ``calmar``, ``sharpe_dd``
(Sharpe less a penalty per unit of max drawdown) and ``multi`` (a weighted
sum of several metrics, whose components also feed a Pareto search, see
``lab.tuning.optuna``). Sortino and Calmar are capped at
:data:`RATIO_CAP`: with no downside in the window they are infinite, and
the trial ledger counts a non-finite score as a failed trial.

:data:`OBJECTIVES` is the registry of plain objectives by name.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
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
            metrics=self.components(report),
        )

    def components(self, report: BacktestReport) -> dict[str, float] | None:
        """Named parts of the score (multi-metric objectives); ``None``."""
        return None

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


#: Where Sortino and Calmar stop: beyond it the window had no real downside.
RATIO_CAP = 100.0


def _capped(value: float) -> float:
    if math.isnan(value):
        return value
    return max(-RATIO_CAP, min(RATIO_CAP, value))


class SortinoObjective(_BacktestObjective):
    name = "sortino"
    direction: Literal["maximize", "minimize"] = "maximize"

    def metric(self, report: BacktestReport) -> float:
        return _capped(report.sortino)


class CalmarObjective(_BacktestObjective):
    name = "calmar"
    direction: Literal["maximize", "minimize"] = "maximize"

    def metric(self, report: BacktestReport) -> float:
        return _capped(report.calmar)


class DrawdownSharpeObjective(_BacktestObjective):
    """Sharpe less ``penalty`` times the max drawdown (a fraction), so two
    sets with the same Sharpe rank by the pain on the way."""

    name = "sharpe_dd"
    direction: Literal["maximize", "minimize"] = "maximize"

    def __init__(self, penalty: float = 2.0) -> None:
        if penalty < 0 or not math.isfinite(penalty):
            raise ValueError(f"penalty must be a finite number >= 0, got {penalty}")
        self.penalty = float(penalty)

    def metric(self, report: BacktestReport) -> float:
        return report.sharpe - self.penalty * abs(report.max_drawdown)


_METRICS: dict[str, Callable[[BacktestReport], float]] = {
    "sharpe": lambda r: r.sharpe,
    "sortino": lambda r: _capped(r.sortino),
    "calmar": lambda r: _capped(r.calmar),
    "cagr": lambda r: r.cagr,
    "final_return": lambda r: r.final_return,
    "drawdown": lambda r: abs(r.max_drawdown),
    "ulcer_index": lambda r: r.ulcer_index,
}


def metric_value(report: BacktestReport, name: str) -> float:
    """One named metric of ``report`` (``drawdown`` is the max drawdown as a
    positive fraction)."""
    try:
        return float(_METRICS[name](report))
    except KeyError:
        raise ValueError(f"unknown metric {name!r}; choose from {sorted(_METRICS)}") from None


#: Sharpe first, some Calmar, and a charge for the deepest drawdown.
DEFAULT_MULTI_WEIGHTS: dict[str, float] = {"sharpe": 1.0, "calmar": 0.5, "drawdown": -1.0}


class MultiMetricObjective(_BacktestObjective):
    """A weighted sum of named metrics (:func:`metric_value`). A negative
    weight means lower is better. ``components`` gives each metric, and
    ``metric_names`` with ``directions`` let a Pareto tuner (NSGA-II) search
    them jointly and use the weighted sum only to pick from the front."""

    name = "multi"
    direction: Literal["maximize", "minimize"] = "maximize"

    def __init__(self, weights: Mapping[str, float] | None = None) -> None:
        chosen = dict(DEFAULT_MULTI_WEIGHTS if weights is None else weights)
        if not chosen:
            raise ValueError("give at least one metric weight")
        unknown = sorted(set(chosen) - set(_METRICS))
        if unknown:
            raise ValueError(f"unknown metric {unknown}; choose from {sorted(_METRICS)}")
        if any(w == 0 or not math.isfinite(w) for w in chosen.values()):
            raise ValueError("every weight must be finite and non-zero")
        self.weights = chosen

    @property
    def metric_names(self) -> list[str]:
        return list(self.weights)

    @property
    def directions(self) -> list[Literal["maximize", "minimize"]]:
        return ["maximize" if w > 0 else "minimize" for w in self.weights.values()]

    def components(self, report: BacktestReport) -> dict[str, float]:
        return {name: metric_value(report, name) for name in self.weights}

    def metric(self, report: BacktestReport) -> float:
        parts = self.components(report)
        return float(sum(w * parts[name] for name, w in self.weights.items()))


#: Plain objectives by name (the ``cv_*`` variants wrap these, see
#: ``app.lab``).
OBJECTIVES: dict[str, Callable[[], _BacktestObjective]] = {
    "sharpe": SharpeObjective,
    "cagr": CAGRObjective,
    "final_return": FinalReturnObjective,
    "sortino": SortinoObjective,
    "calmar": CalmarObjective,
    "sharpe_dd": DrawdownSharpeObjective,
    "multi": MultiMetricObjective,
}
