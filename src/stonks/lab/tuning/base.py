"""Tuner helpers shared by concrete implementations."""

from __future__ import annotations

import itertools
import math
import random
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from stonks.core.params import ParameterSpec, ParamSpace, tunable_only
from stonks.core.protocols import Strategy, TunerResult

if TYPE_CHECKING:  # pragma: no cover
    from stonks.lab.survival.base import TuningSetup


def tune_and_fit(
    strategy_cls: type[Strategy], dataset: Any, setup: TuningSetup
) -> tuple[Strategy, TunerResult]:
    """Tune ``strategy_cls`` on ``dataset``'s train window, then build and
    fit the winning configuration on the same dataset. The one tune → fit
    sequence shared by the runner and every re-tuning survival test."""
    tuned = setup.tuner.tune(
        strategy_cls=strategy_cls,
        param_space=strategy_cls.parameter_spec(),
        objective=setup.objective,
        dataset=dataset,
        budget=setup.budget,
    )
    strategy = strategy_cls(tuned.best_params)
    strategy.fit(dataset)  # no-op for rule-based
    return strategy, tuned


def expand_grid(space: ParamSpace, grid_size: int) -> Iterable[dict[str, Any]]:
    """Yield every combination of parameter values from the tunable part of
    ``space``, discretizing numeric bounds into ``grid_size`` evenly-spaced
    points. Non-tunable params are filled from their defaults by the caller.
    """
    axes = grid_axes(space, grid_size)
    names = [n for n, _ in axes]
    for combo in itertools.product(*(v for _, v in axes)):
        yield dict(zip(names, combo, strict=False))


def grid_axes(space: ParamSpace, grid_size: int) -> list[tuple[str, list[Any]]]:
    """``(name, values)`` per tunable parameter, in spec order."""
    return [(spec.name, _axis_values(spec, grid_size)) for spec in tunable_only(space)]


def grid_size_of(axes: list[tuple[str, list[Any]]]) -> int:
    return math.prod(len(values) for _, values in axes)


def sample_grid(
    axes: list[tuple[str, list[Any]]], k: int, rng: random.Random
) -> list[dict[str, Any]]:
    """Draw ``k`` distinct grid combinations uniformly at random.

    Combinations are addressed by their flat index into the cartesian
    product and decoded mixed-radix, so the full grid is never
    materialized. Returned in grid order for readable trial logs.
    """
    total = grid_size_of(axes)
    indices = sorted(rng.sample(range(total), min(k, total)))
    return [_decode(i, axes) for i in indices]


def _decode(index: int, axes: list[tuple[str, list[Any]]]) -> dict[str, Any]:
    combo: dict[str, Any] = {}
    for name, values in reversed(axes):
        index, pos = divmod(index, len(values))
        combo[name] = values[pos]
    return {name: combo[name] for name, _ in axes}


def _axis_values(spec: ParameterSpec, grid_size: int) -> list[Any]:
    if spec.kind == "categorical":
        return list(spec.bounds or [])
    if spec.kind == "bool":
        return [False, True]
    lo, hi = spec.bounds  # numeric
    if grid_size <= 1:
        return [spec.default]
    step = (hi - lo) / (grid_size - 1)
    points = [lo + step * i for i in range(grid_size)]
    if spec.kind == "int":
        return sorted({int(round(p)) for p in points})
    return points


def merge_with_defaults(params: dict[str, Any], space: ParamSpace) -> dict[str, Any]:
    """Fill any missing params with their ParameterSpec defaults."""
    defaults = {s.name: s.default for s in space}
    return {**defaults, **params}
