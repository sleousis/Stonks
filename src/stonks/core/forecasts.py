"""The probability forecast seam (roadmap 23.9).

A strategy whose model is a classifier can say what probability it gives
an event (a meta-label's ``P(win)`` for the trade now open) and, later,
whether the event happened. The model lifecycle records both for every
model version running as a model book and scores their calibration
(``lifecycle.calibration``). A strategy opts in by having both methods.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

__all__ = ["ProbabilityForecast", "ProbabilityForecaster"]


@dataclass(frozen=True)
class ProbabilityForecast:
    #: Names the event, stable across days (e.g. the trade's entry time),
    #: so a forecast repeated while the event is open counts once.
    event_key: str
    probability: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.probability <= 1.0:
            raise ValueError(f"probability must lie in [0, 1], got {self.probability}")


@runtime_checkable
class ProbabilityForecaster(Protocol):
    def forecast_probability(
        self, ticker: str, as_of: Any, lake: Any
    ) -> ProbabilityForecast | None:
        """The forecast for ``ticker`` at ``as_of`` (bars ``<= as_of``),
        else ``None`` when the model has nothing to say."""
        ...

    def forecast_outcome(self, ticker: str, event_key: str, as_of: Any, lake: Any) -> bool | None:
        """Whether event ``event_key`` happened, known from bars ``<= as_of``;
        ``None`` while it is still open."""
        ...
