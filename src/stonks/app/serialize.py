"""Turn service results into JSON-safe primitives."""

from __future__ import annotations

import dataclasses
import math
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator


def finite(value: float | None) -> float | None:
    """``None`` for NaN / +-inf, which JSON cannot represent."""
    if value is None:
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _finite_or_passthrough(value: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value  # let the field's own validation judge it
    return finite(value)


#: A response-model float that is ``null`` when non-finite (NaN, +-inf), so
#: strict JSON (``allow_nan=False``) never fails and the schema says so.
FiniteFloat = Annotated[float | None, BeforeValidator(_finite_or_passthrough)]


def to_jsonable(value: Any) -> Any:
    """Recursively convert models / dataclasses / dates into JSON primitives.

    Non-finite floats become ``None`` so the result always round-trips
    through ``json.dumps(..., allow_nan=False)``.
    """
    if isinstance(value, BaseModel):
        return to_jsonable(value.model_dump(mode="python"))
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return to_jsonable(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return finite(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return value.as_posix()
    if hasattr(value, "item"):  # numpy scalars
        return to_jsonable(value.item())
    return str(value)
