"""Survival-suite scaffolding. SurvivalTest is defined in core.protocols as a
structural Protocol; this module just holds the composition helper."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from stonks.core.protocols import Objective, Strategy, SurvivalReport, SurvivalTest, Tuner


@dataclass(frozen=True)
class TuningSetup:
    """How a survival test re-tunes a strategy: the runner's tuner,
    objective and trial budget. ``LabRunner`` hands its own setup to every
    test exposing ``bind_tuning(setup)`` (walk-forward, re-tuning MCPT)."""

    tuner: Tuner
    objective: Objective
    budget: int


class SurvivalSuite:
    def __init__(self, tests: Sequence[SurvivalTest]) -> None:
        self._tests = list(tests)

    def run(self, strategy: Strategy, context: Any) -> list[SurvivalReport]:
        return [t.run(strategy, context) for t in self._tests]

    @property
    def tests(self) -> list[SurvivalTest]:
        return list(self._tests)
