"""Survival-suite scaffolding. SurvivalTest is defined in core.protocols as a
structural Protocol; this module just holds the composition helper."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from stonks.core.protocols import Objective, Strategy, SurvivalReport, SurvivalTest, Tuner
from stonks.lab.tuning.base import fixed_params_of


@dataclass(frozen=True)
class TuningSetup:
    """How a survival test re-tunes a strategy: the runner's tuner,
    objective and trial budget. ``LabRunner`` hands its own setup to every
    test exposing ``bind_tuning(setup)`` (walk-forward, re-tuning MCPT)."""

    tuner: Tuner
    objective: Objective
    budget: int
    #: Params the caller pinned for the runner's own tuning. Re-tuning tests
    #: keep them pinned too (on top of the strategy's non-tunable params,
    #: see ``retune_fixed_params``) so they validate the same search.
    fixed_params: Mapping[str, Any] = field(default_factory=dict)

    def retune_fixed_params(self, strategy: Strategy) -> dict[str, Any]:
        """What a re-tune of ``strategy`` must hold fixed: its non-tunable
        params (e.g. a wrapper's inner strategy) plus the pinned ones."""
        return {**fixed_params_of(strategy), **self.fixed_params}


class SurvivalSuite:
    def __init__(self, tests: Sequence[SurvivalTest]) -> None:
        self._tests = list(tests)

    def run(self, strategy: Strategy, context: Any) -> list[SurvivalReport]:
        return [t.run(strategy, context) for t in self._tests]

    @property
    def tests(self) -> list[SurvivalTest]:
        return list(self._tests)
