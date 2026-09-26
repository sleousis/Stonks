"""Grid search over the tunable part of a parameter space.

Exhaustive when the grid fits in the budget; otherwise a seeded uniform
sample of distinct grid points (so every axis is explored, not just the
corner that ``itertools.product`` would visit first).

Candidates are fixed up front, then evaluated through the
``lab.parallel`` pool (see ``lab.tuning.base.evaluate_candidates``); the
result is identical for any worker count.
"""

from __future__ import annotations

import random

from stonks.core.params import ParamSpace
from stonks.core.protocols import Objective, Strategy, TunerResult
from stonks.lab.dataset import LabDataset
from stonks.lab.parallel import ParallelSettings
from stonks.lab.tuning.base import (
    best_of,
    evaluate_candidates,
    expand_grid,
    grid_axes,
    grid_size_of,
    merge_with_defaults,
    sample_grid,
)
from stonks.logging import get_logger

_log = get_logger("stonks.lab.tuning.grid")


class GridTuner:
    def __init__(
        self, grid_size: int = 5, seed: int = 0, parallel: ParallelSettings | None = None
    ) -> None:
        self._grid_size = grid_size
        #: Picks the sample when the grid is larger than the budget, and
        #: roots the per-trial seeds.
        self._seed = seed
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
        axes = grid_axes(param_space, self._grid_size)
        total = grid_size_of(axes)
        if total > budget:
            _log.warning(
                "grid.truncated",
                grid_points=total,
                budget=budget,
                seed=self._seed,
            )
            candidates = sample_grid(axes, budget, random.Random(self._seed))
        else:
            candidates = list(expand_grid(param_space, self._grid_size))

        trials = evaluate_candidates(
            strategy_cls,
            [merge_with_defaults(c, param_space) for c in candidates],
            objective,
            dataset,
            parallel=self._parallel,
            root_seed=self._seed,
            log_prefix="grid",
        )
        best_params, best_score = best_of(trials, objective.direction, param_space)
        return TunerResult(
            best_params=best_params,
            best_score=best_score,
            history=[(t.params, t.score) for t in trials],
            trials=trials,
        )
