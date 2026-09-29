"""Typed views of eToro's JSON: an object, a list of objects, a number.

eToro's answers are parsed defensively. A field of the wrong shape reads as
empty, never as an error deep in a mapping.
"""

from __future__ import annotations

import math
from typing import Any, cast


def obj(value: object) -> dict[str, Any]:
    """``value`` when it is a JSON object, else an empty one."""
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def rows(value: object) -> list[dict[str, Any]]:
    """The JSON objects in ``value`` when it is a list, else none."""
    if not isinstance(value, list):
        return []
    return [cast(dict[str, Any], v) for v in cast(list[object], value) if isinstance(v, dict)]


def strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v for v in cast(list[object], value) if isinstance(v, str)]


def num(value: object) -> float | None:
    """A finite float, or ``None``."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        out = float(cast(Any, value))
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def integer(value: object, *, default: int = -1) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(cast(Any, value))
    except (TypeError, ValueError):
        return default


__all__ = ["integer", "num", "obj", "rows", "strings"]
