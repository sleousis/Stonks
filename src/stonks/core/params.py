"""Declarative parameter surface shared by strategies and tuners.

A strategy lists its parameters via `ParameterSpec` entries. Tuners read only the
spec list, never the strategy internals, keeping the two independent.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from numbers import Integral, Real
from typing import Any, Literal

ParamKind = Literal["float", "int", "categorical", "bool"]


@dataclass(frozen=True)
class ParameterSpec:
    name: str
    kind: ParamKind
    default: Any
    bounds: tuple[Any, Any] | list[Any] | None = None
    tunable: bool = True
    description: str = ""


Params = Mapping[str, Any]
ParamSpace = Sequence[ParameterSpec]


def tunable_only(space: ParamSpace) -> list[ParameterSpec]:
    """Return only the tunable entries of the space, preserving order."""
    return [s for s in space if s.tunable]


def validate_params(params: Params, space: ParamSpace) -> None:
    """Raise ValueError if `params` is not consistent with `space`.

    Rules:
      - every key in `params` must have a matching spec by name
      - the value's type must match spec.kind (numpy scalars count:
        ``np.int64`` is an int, ``np.float64`` a float, ``np.bool_`` a bool)
      - numeric values must fall inside spec.bounds (when bounds is set)
      - categorical values must be in spec.bounds
    Missing keys are allowed; callers are expected to fill defaults.
    """
    specs_by_name = {s.name: s for s in space}
    for name, value in params.items():
        if name not in specs_by_name:
            raise ValueError(f"unknown parameter {name!r}")
        spec = specs_by_name[name]

        if spec.kind == "bool":
            if not _is_bool(value):
                raise ValueError(f"{name}: expected bool, got {type(value).__name__}")
            continue

        if spec.kind == "int":
            if _is_bool(value) or not isinstance(value, Integral):
                raise ValueError(f"{name}: expected int, got {type(value).__name__}")
            _check_numeric_bounds(name, value, spec.bounds)
            continue

        if spec.kind == "float":
            if _is_bool(value) or not isinstance(value, Real):
                raise ValueError(f"{name}: expected float, got {type(value).__name__}")
            _check_numeric_bounds(name, value, spec.bounds)
            continue

        if spec.kind == "categorical":
            # ``bounds=None`` on a categorical = no closed choice set, any
            # string is accepted. This is what single-ticker strategies use
            # for their ``ticker`` parameter (the set of valid tickers is
            # the lake's universe, which is not declarable at the spec).
            if spec.bounds is None:
                continue
            if value not in spec.bounds:
                raise ValueError(f"{name}={value!r} not in choices {list(spec.bounds)}")
            continue


def _is_bool(value: Any) -> bool:
    """A Python or numpy bool (``np.bool_`` is not a Python ``bool``)."""
    return isinstance(value, bool) or type(value).__name__ in ("bool_", "bool")


def _check_numeric_bounds(name: str, value: int | float, bounds: Any) -> None:
    if bounds is None:
        return
    lo, hi = bounds
    if not (lo <= value <= hi):
        raise ValueError(f"{name}={value} out of bounds [{lo}, {hi}]")
