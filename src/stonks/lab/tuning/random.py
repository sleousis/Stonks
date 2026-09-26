"""Random search over the tunable part of a parameter space.

All ``budget`` candidates are drawn up front from ``seed``, then evaluated
through the ``lab.parallel`` pool (see
``lab.tuning.base.evaluate_candidates``); the result is identical for any
worker count.
"""

from __future__ import annotations

import random
from typing import Any

from stonks.core.params import ParameterSpec, ParamSpace, tunable_only
from stonks.core.protocols import Objective, Strategy, TunerResult
from stonks.lab.dataset import LabDataset
from stonks.lab.parallel import ParallelSettings
from stonks.lab.tuning.base import best_of, evaluate_candidates, merge_with_defaults
from stonks.logging import get_logger

_log = get_logger("stonks.lab.tuning.random")


class RandomTuner:
    def __init__(self, seed: int = 0, parallel: ParallelSettings | None = None) -> None:
        # Fixed default seed so a lab run is reproducible unless the caller
        # explicitly asks for a different draw.
        self._seed = seed
        # the rng is re-created per tune() call (see tune), so every call
        # draws the same candidates whatever ran before it
        self._rng = random.Random(seed)
        #: Default: every core (``ParallelSettings()``).
        self._parallel = parallel or ParallelSettings()

    def tune(
        self,
        strategy_cls: type[Strategy],
        param_space: ParamSpace,
        objective: Objective,
        dataset: LabDataset,
        budget: int,
    ) -> TunerResult:
        tunable = tunable_only(param_space)
        self._rng = random.Random(self._seed)
        _log.info("random.tune.start", seed=self._seed, budget=budget)
        # every slot is a trial, failed or not — otherwise budget semantics drift
        candidates = [
            merge_with_defaults({s.name: self._sample(s) for s in tunable}, param_space)
            for _ in range(budget)
        ]
        trials = evaluate_candidates(
            strategy_cls,
            candidates,
            objective,
            dataset,
            parallel=self._parallel,
            root_seed=self._seed,
            log_prefix="random",
        )
        best_params, best_score = best_of(trials, objective.direction, param_space)
        return TunerResult(
            best_params=best_params,
            best_score=best_score,
            history=[(t.params, t.score) for t in trials],
            trials=trials,
        )

    def _sample(self, spec: ParameterSpec) -> Any:
        if spec.kind == "categorical":
            return self._rng.choice(list(spec.bounds or [spec.default]))
        if spec.kind == "bool":
            return self._rng.random() < 0.5
        lo, hi = spec.bounds
        if spec.kind == "int":
            return self._rng.randint(int(lo), int(hi))
        return self._rng.uniform(float(lo), float(hi))
