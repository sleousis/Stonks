"""Protocol surface that every Strategy / Tuner / Objective / SurvivalTest /
Broker implementation must satisfy.

These are typing Protocols (structural) — no inheritance needed. The point is
that new strategies don't touch the tuner and new tuners don't touch any
strategy; both sides only know the Protocol.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

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

    def estimate_return(
        self, ticker: str, as_of: date, lake: DuckDBLake
    ) -> float | None: ...

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


@dataclass(frozen=True)
class TunerResult:
    best_params: Params
    best_score: float
    history: list[tuple[Params, float]]


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
