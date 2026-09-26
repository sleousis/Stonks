"""Random search over the tunable part of a parameter space."""

from __future__ import annotations

import math
import random
from typing import Any

from stonks.core.params import ParameterSpec, ParamSpace, tunable_only
from stonks.core.protocols import Objective, Strategy, TunerResult
from stonks.lab.dataset import LabDataset
from stonks.lab.tuning.base import merge_with_defaults
from stonks.logging import get_logger

_log = get_logger("stonks.lab.tuning.random")


class RandomTuner:
    def __init__(self, seed: int = 0) -> None:
        # Fixed default seed so a lab run is reproducible unless the caller
        # explicitly asks for a different draw.
        self._seed = seed
        # the rng is re-created per tune() call (see tune), so every call
        # draws the same candidates whatever ran before it
        self._rng = random.Random(seed)

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

        tunable = tunable_only(param_space)
        self._rng = random.Random(self._seed)
        _log.info("random.tune.start", seed=self._seed, budget=budget)
        for _ in range(budget):
            partial = {s.name: self._sample(s) for s in tunable}
            params = merge_with_defaults(partial, param_space)
            try:
                strategy = strategy_cls(params)
                strategy.fit(dataset)  # same call LabRunner makes after tuning
                score = objective.score(strategy, dataset)
            except Exception as exc:
                _log.warning("random.trial.failed", params=params, error=str(exc))
                # still count as a trial slot — otherwise budget semantics drift
                history.append((params, float("nan")))
                continue
            history.append((params, score))
            if not math.isnan(score) and _better(score, best_score, objective.direction):
                best_score = score
                best_params = params

        if best_params is None:
            best_params = merge_with_defaults({}, param_space)
            best_score = 0.0

        return TunerResult(best_params=best_params, best_score=best_score, history=history)

    def _sample(self, spec: ParameterSpec) -> Any:
        if spec.kind == "categorical":
            return self._rng.choice(list(spec.bounds or [spec.default]))
        if spec.kind == "bool":
            return self._rng.random() < 0.5
        lo, hi = spec.bounds
        if spec.kind == "int":
            return self._rng.randint(int(lo), int(hi))
        return self._rng.uniform(float(lo), float(hi))


def _better(candidate: float, incumbent: float, direction: str) -> bool:
    if direction == "maximize":
        return candidate > incumbent
    return candidate < incumbent
