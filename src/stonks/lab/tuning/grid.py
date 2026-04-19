"""Exhaustive grid search over the tunable part of a parameter space."""

from __future__ import annotations

import math
from typing import Any

from stonks.core.params import ParamSpace
from stonks.core.protocols import Objective, Strategy, TunerResult
from stonks.lab.dataset import LabDataset
from stonks.lab.tuning.base import expand_grid, merge_with_defaults
from stonks.logging import get_logger

_log = get_logger("stonks.lab.tuning.grid")


class GridTuner:
    def __init__(self, grid_size: int = 5) -> None:
        self._grid_size = grid_size

    def tune(
        self,
        strategy_cls: type[Strategy],
        param_space: ParamSpace,
        objective: Objective,
        dataset: LabDataset,
        budget: int,
    ) -> TunerResult:
        history: list[tuple[dict[str, Any], float]] = []
        best_params: dict[str, Any] | None = None
        best_score = -math.inf if objective.direction == "maximize" else math.inf

        for trial, partial in enumerate(expand_grid(param_space, self._grid_size)):
            if trial >= budget:
                break
            params = merge_with_defaults(partial, param_space)
            strategy = strategy_cls(params)
            try:
                score = objective.score(strategy, dataset)
            except Exception as exc:
                _log.warning("grid.trial.failed", params=params, error=str(exc))
                history.append((params, float("nan")))
                continue
            history.append((params, score))
            if _better(score, best_score, objective.direction):
                best_score = score
                best_params = params

        if best_params is None:  # nothing evaluated
            best_params = merge_with_defaults({}, param_space)
            best_score = 0.0

        return TunerResult(best_params=best_params, best_score=best_score, history=history)


def _better(candidate: float, incumbent: float, direction: str) -> bool:
    if direction == "maximize":
        return candidate > incumbent
    return candidate < incumbent
