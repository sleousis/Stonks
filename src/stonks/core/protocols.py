"""Protocol surface that every Strategy / Tuner / Objective / SurvivalTest /
Broker implementation must satisfy.

These are typing Protocols (structural) — no inheritance needed. The point is
that new strategies don't touch the tuner and new tuners don't touch any
strategy; both sides only know the Protocol.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

import numpy as np

from stonks.core.params import Params, ParamSpace
from stonks.core.types import Features, Fill, Order, Portfolio

if TYPE_CHECKING:  # pragma: no cover
    from stonks.store.lake import DuckDBLake


@runtime_checkable
class Strategy(Protocol):
    id: str

    @classmethod
    def parameter_spec(cls) -> ParamSpace: ...

    def __init__(self, params: Params) -> None: ...

    def extract_features(self, ticker: str, as_of: date, lake: DuckDBLake) -> Features: ...

    def fit(self, dataset: Any) -> None: ...

    def estimate_return(self, ticker: str, as_of: date, lake: DuckDBLake) -> float | None: ...

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of: date,
    ) -> list[Order]: ...

    def save(self, path: Path) -> None: ...

    @classmethod
    def load(cls, path: Path) -> Strategy: ...


@runtime_checkable
class Broker(Protocol):
    def fetch_portfolio(self) -> Portfolio: ...
    def place_order(self, order: Order) -> Fill | None: ...
    def reconcile(self) -> list[Fill]: ...


@runtime_checkable
class Objective(Protocol):
    name: str
    direction: Literal["maximize", "minimize"]

    def score(self, strategy: Strategy, dataset: Any) -> float: ...


@dataclass(frozen=True, eq=False)
class TrialOutcome:
    """One tuning trial: the params tried, the objective's score and, when
    the objective provides them, the per-bar returns behind that score
    (``index`` holds their bar timestamps as ``datetime64[ns]``).

    A failed trial has ``status="failed"``, a NaN score and no returns.
    Equality is by value, NaN equal to NaN, so reruns can be compared."""

    params: Params
    score: float
    returns: np.ndarray | None = None
    index: np.ndarray | None = None
    status: Literal["ok", "failed"] = "ok"
    error: str | None = None
    #: Named parts of the score, when the objective has several (a
    #: multi-metric objective); a Pareto tuner reads them.
    metrics: Mapping[str, float] | None = None

    @property
    def n_bars(self) -> int:
        return 0 if self.returns is None else len(self.returns)

    @classmethod
    def failed(cls, params: Params, error: str) -> TrialOutcome:
        return cls(params=params, score=float("nan"), status="failed", error=error)

    def with_params(self, params: Params) -> TrialOutcome:
        return replace(self, params=params)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, TrialOutcome):
            return NotImplemented
        return (
            dict(self.params) == dict(other.params)
            and _same_float(self.score, other.score)
            and self.status == other.status
            and self.error == other.error
            and _same_array(self.returns, other.returns)
            and _same_array(self.index, other.index)
            and _same_metrics(self.metrics, other.metrics)
        )

    __hash__ = None  # type: ignore[assignment]  # mutable payload (arrays)


def _same_float(a: float, b: float) -> bool:
    return a == b or (math.isnan(a) and math.isnan(b))


def _same_metrics(a: Mapping[str, float] | None, b: Mapping[str, float] | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return a.keys() == b.keys() and all(_same_float(a[k], b[k]) for k in a)


def _same_array(a: np.ndarray | None, b: np.ndarray | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if a.dtype.kind == "f" and b.dtype.kind == "f":
        return bool(np.array_equal(a, b, equal_nan=True))
    return bool(np.array_equal(a, b))


@dataclass(frozen=True)
class TunerResult:
    best_params: Params
    best_score: float
    history: list[tuple[Params, float]]
    #: Every trial in ``history`` order, when the tuner records outcomes
    #: (``GridTuner`` / ``RandomTuner`` do); ``None`` for tuners that don't.
    trials: list[TrialOutcome] | None = None


@runtime_checkable
class Tuner(Protocol):
    def tune(
        self,
        strategy_cls: type[Strategy],
        param_space: ParamSpace,
        objective: Objective,
        dataset: Any,
        budget: int,
    ) -> TunerResult: ...


@dataclass(frozen=True)
class SurvivalReport:
    test_id: str
    passed: bool
    metrics: Mapping[str, float]
    notes: str = ""


@runtime_checkable
class SurvivalTest(Protocol):
    id: str

    def run(self, strategy: Strategy, context: Any) -> SurvivalReport: ...
