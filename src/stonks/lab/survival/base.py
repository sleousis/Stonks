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
    """Runs its tests and returns their reports in the order given.

    A test that leaves a result on the dataset for later tests (it sets the
    class attribute ``publishes_to_dataset = True``, as ``walk_forward``
    does with its stitched OOS report) runs before the others, so a
    consumer such as ``mc_trades`` sees it wherever it is listed."""

    def __init__(self, tests: Sequence[SurvivalTest]) -> None:
        self._tests = list(tests)

    def run(self, strategy: Strategy, context: Any) -> list[SurvivalReport]:
        order = sorted(
            range(len(self._tests)),
            key=lambda i: not getattr(self._tests[i], "publishes_to_dataset", False),
        )
        reports: dict[int, SurvivalReport] = {}
        for i in order:
            reports[i] = self._tests[i].run(strategy, context)
        return [reports[i] for i in range(len(self._tests))]

    @property
    def tests(self) -> list[SurvivalTest]:
        return list(self._tests)
