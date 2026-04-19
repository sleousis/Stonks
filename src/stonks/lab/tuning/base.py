"""Tuner helpers shared by concrete implementations."""

from __future__ import annotations

import itertools
from collections.abc import Iterable
from typing import Any

from stonks.core.params import ParameterSpec, ParamSpace, tunable_only


def expand_grid(space: ParamSpace, grid_size: int) -> Iterable[dict[str, Any]]:
    """Yield every combination of parameter values from the tunable part of
    ``space``, discretizing numeric bounds into ``grid_size`` evenly-spaced
    points. Non-tunable params are filled from their defaults by the caller.
    """
    tunable = tunable_only(space)
    axes: list[tuple[str, list[Any]]] = []
    for spec in tunable:
        values = _axis_values(spec, grid_size)
        axes.append((spec.name, values))
    names = [n for n, _ in axes]
    for combo in itertools.product(*(v for _, v in axes)):
        yield dict(zip(names, combo, strict=False))


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
