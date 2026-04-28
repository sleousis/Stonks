"""Lab orchestration: tune → fit → survival suite → verdict."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from stonks.core.protocols import (
    Objective,
    Strategy,
    SurvivalReport,
    Tuner,
)
from stonks.lab.dataset import LabDataset
from stonks.lab.survival.base import SurvivalSuite
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

    def run(self, strategy_cls: type[Strategy], dataset: LabDataset) -> LabRunResult:
        space = strategy_cls.parameter_spec()
        tuned = self._tuner.tune(
            strategy_cls=strategy_cls,
            param_space=space,
            objective=self._objective,
            dataset=dataset,
            budget=self._budget,
        )
        _log.info(
            "lab.tune.done",
            best_params=tuned.best_params,
            best_score=tuned.best_score,
            trials=len(tuned.history),
        )

        strategy = strategy_cls(tuned.best_params)
        strategy.fit(dataset)  # no-op for rule-based

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
