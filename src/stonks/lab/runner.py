"""Lab orchestration: tune → fit → survival suite → verdict."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from stonks.core.protocols import (
    Objective,
    Strategy,
    SurvivalReport,
    Tuner,
)
from stonks.lab.dataset import LabDataset
from stonks.lab.survival.base import SurvivalSuite, TuningSetup
from stonks.lab.tuning.base import tune_and_fit
from stonks.logging import get_logger

_log = get_logger("stonks.lab.runner")


@dataclass
class LabRunResult:
    strategy_cls: type[Strategy]
    best_params: dict[str, Any]
    best_score: float
    strategy: Strategy
    survival_reports: list[SurvivalReport]
    verdict: str  # "pass" | "fail"


class LabRunner:
    def __init__(
        self,
        tuner: Tuner,
        objective: Objective,
        suite: SurvivalSuite,
        budget: int = 20,
    ) -> None:
        self._tuner = tuner
        self._objective = objective
        self._suite = suite
        self._budget = budget

    def run(
        self,
        strategy_cls: type[Strategy],
        dataset: LabDataset,
        fixed_params: Mapping[str, Any] | None = None,
    ) -> LabRunResult:
        """``fixed_params`` pin params for tuning (e.g. a
        ``MacroRegimeFilter``'s inner strategy, a ticker); survival tests
        that re-tune keep them pinned, plus the strategy's non-tunable params."""
        setup = TuningSetup(
            tuner=self._tuner,
            objective=self._objective,
            budget=self._budget,
            fixed_params=dict(fixed_params or {}),
        )
        strategy, tuned = tune_and_fit(strategy_cls, dataset, setup, setup.fixed_params)
        _log.info(
            "lab.tune.done",
            best_params=tuned.best_params,
            best_score=tuned.best_score,
            trials=len(tuned.history),
        )

        # Tests that re-tune (walk-forward, re-tuning MCPT) use the same
        # tuner / objective / budget that picked ``strategy``.
        for test in self._suite.tests:
            bind = getattr(test, "bind_tuning", None)
            if callable(bind):
                bind(setup)

        reports = self._suite.run(strategy, dataset)
        verdict = "pass" if all(r.passed for r in reports) else "fail"

        _log.info(
            "lab.survival.done",
            verdict=verdict,
            reports=[{"test": r.test_id, "passed": r.passed} for r in reports],
        )

        return LabRunResult(
            strategy_cls=strategy_cls,
            best_params=dict(tuned.best_params),
            best_score=tuned.best_score,
            strategy=strategy,
            survival_reports=list(reports),
            verdict=verdict,
        )
