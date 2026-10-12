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

:func:`register_objective` is the registry: one decorated class is an
objective every transport (API, CLI, MCP, web) accepts by name, and
``cv=True`` adds its ``cv_<name>`` variant (``lab.cv.CVObjective``) too.
Outside packages add objectives through the ``stonks.objectives`` entry
point group (``lab.plugins``). The first line of a class's docstring is its
label in the console, the next paragraph its description.
"""

from __future__ import annotations

import inspect
import math
from collections.abc import Callable, Mapping
from typing import Any, Literal, TypeVar

import numpy as np

from stonks.backtest.report import BacktestReport
from stonks.core.protocols import Strategy, TrialOutcome
from stonks.lab.backtesting import run_backtest as _backtest
from stonks.lab.dataset import LabDataset
from stonks.logging import get_logger

_log = get_logger("stonks.lab.objectives")

_T = TypeVar("_T", bound=type)

#: Plain objectives by name, filled by :func:`register_objective`.
_PLAIN: dict[str, type] = {}
#: Names whose ``cv_`` variant is offered too.
_CV: list[str] = []
#: Prefix of the cross-validated variants.
CV_PREFIX = "cv_"


def register_objective(name: str, *, cv: bool = False) -> Callable[[_T], _T]:
    """Class decorator: register an objective under ``name`` (also set as
    its ``name``). ``cv=True`` offers ``cv_<name>`` as well, the same
    metric averaged over purged folds. Two classes cannot share a name."""

    def decorate(cls: _T) -> _T:
        existing = _PLAIN.get(name)
        if existing is not None and existing is not cls:
            raise ValueError(f"objective {name!r} is already registered by {existing.__qualname__}")
        if name.startswith(CV_PREFIX):
            raise ValueError(f"objective names cannot start with {CV_PREFIX!r}: {name!r}")
        cls.name = name  # type: ignore[attr-defined]
        _PLAIN[name] = cls
        if cv and name not in _CV:
            _CV.append(name)
        return cls

    return decorate


def _load_plugins() -> None:
    """Register the ``stonks.objectives`` entry points not registered yet.
    A plugin never replaces a built-in."""
    from stonks.lab import plugins

    for plugin in plugins.plugin_objects(plugins.OBJECTIVES):
        cls = plugin.obj
        if not isinstance(cls, type):
            continue  # a module: importing it ran its decorators
        if cls in _PLAIN.values():
            continue
        if plugin.name in _PLAIN or plugin.name.startswith(CV_PREFIX):
            _log.warning("lab.plugin.name_taken", group=plugin.group, name=plugin.name)
            continue
        register_objective(plugin.name, cv=bool(getattr(cls, "cv", False)))(cls)


def objective_factories() -> dict[str, Callable[[], Any]]:
    """Every objective a lab request can name, ``name -> factory``, the
    ``cv_`` variants included: the plain ones in registration order, then
    the ``cv_`` ones (the order menus show)."""
    _load_plugins()
    out: dict[str, Callable[[], Any]] = dict(_PLAIN)
    for name in _CV:
        out[f"{CV_PREFIX}{name}"] = _cv_factory(_PLAIN[name])
    return out


def _cv_factory(cls: type) -> Callable[[], Any]:
    def make() -> Any:
        from stonks.lab.cv import CVObjective

        return CVObjective(cls())

    return make


def objective_names() -> list[str]:
    """Names of every objective, the ``cv_`` variants included (in the
    order of :func:`objective_factories`)."""
    return list(objective_factories())


def build_objective(name: str) -> Any:
    """A fresh objective named ``name``. Raises ``ValueError`` on a name the
    registry does not know."""
    factories = objective_factories()
    try:
        return factories[name]()
    except KeyError:
        raise ValueError(f"unknown objective {name!r}; choose from {sorted(factories)}") from None


def _doc_parts(name: str) -> tuple[str, str]:
    objective_factories()  # loads plugins
    plain = name.removeprefix(CV_PREFIX) if name not in _PLAIN else name
    cls = _PLAIN.get(plain)
    if cls is None or (plain != name and plain not in _CV):
        raise ValueError(f"unknown objective {name!r}")
    doc = inspect.cleandoc(cls.__dict__.get("__doc__") or "")
    paragraphs = [" ".join(p.split()) for p in doc.split("\n\n") if p.strip()]
    label = paragraphs[0].rstrip(".") if paragraphs else plain.replace("_", " ")
    detail = paragraphs[1] if len(paragraphs) > 1 else ""
    if plain != name:
        label = f"{label} on purged folds"
        detail = (
            f"The {plain} objective averaged over purged folds of the train window, "
            "so the tuner stops picking on in-sample fit."
        )
    return label, detail


def objective_label(name: str) -> str:
    """The short label of objective ``name`` (its docstring's first line)."""
    return _doc_parts(name)[0]


def describe_objective(name: str) -> str:
    """One paragraph on objective ``name`` (its docstring's second)."""
    return _doc_parts(name)[1]


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


@register_objective("sharpe", cv=True)
class SharpeObjective(_BacktestObjective):
    """Sharpe.

    The annualised Sharpe ratio of the backtest on the train window."""

    name = "sharpe"
    direction: Literal["maximize", "minimize"] = "maximize"

    def metric(self, report: BacktestReport) -> float:
        return report.sharpe


@register_objective("cagr", cv=True)
class CAGRObjective(_BacktestObjective):
    """CAGR.

    The compound annual growth rate of the backtest on the train window."""

    name = "cagr"
    direction: Literal["maximize", "minimize"] = "maximize"

    def metric(self, report: BacktestReport) -> float:
        return report.cagr


@register_objective("final_return", cv=True)
class FinalReturnObjective(_BacktestObjective):
    """Total return.

    The total return of the backtest on the train window."""

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


@register_objective("sortino")
class SortinoObjective(_BacktestObjective):
    """Sortino.

    The Sortino ratio on the train window, which only charges downside
    volatility. Capped at RATIO_CAP."""

    name = "sortino"
    direction: Literal["maximize", "minimize"] = "maximize"

    def metric(self, report: BacktestReport) -> float:
        return _capped(report.sortino)


@register_objective("calmar")
class CalmarObjective(_BacktestObjective):
    """Calmar.

    The annual return over the max drawdown on the train window. Capped at
    RATIO_CAP."""

    name = "calmar"
    direction: Literal["maximize", "minimize"] = "maximize"

    def metric(self, report: BacktestReport) -> float:
        return _capped(report.calmar)


@register_objective("sharpe_dd")
class DrawdownSharpeObjective(_BacktestObjective):
    """Sharpe less twice the drawdown.

    Sharpe less ``penalty`` times the max drawdown (a fraction), so two
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


@register_objective("multi")
class MultiMetricObjective(_BacktestObjective):
    """Sharpe, Calmar and drawdown together.

    A weighted sum of named metrics (:func:`metric_value`). A negative
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


#: Plain objectives by name (the registry itself, kept for older callers).
#: Prefer :func:`objective_factories`, which adds plugins and ``cv_*``.
OBJECTIVES: dict[str, Callable[[], _BacktestObjective]] = _PLAIN  # type: ignore[assignment]
