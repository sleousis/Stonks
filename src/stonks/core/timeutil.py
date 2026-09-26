"""Shared date/datetime coercion helpers.

Used by the lake (window bounds), the backtester (pd.Timestamp → datetime),
and strategy examples (as_of normalization) so they don't each carry a
near-identical private copy.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any


def as_datetime(value: Any) -> datetime:
    """Coerce a ``date``, ``datetime``, or ``pd.Timestamp`` to a ``datetime``
    (midnight for plain dates). ``pd.Timestamp`` is converted via
    ``to_pydatetime()``. Anything else is returned unchanged so the caller
    can react — almost always a programmer error upstream.
    """
    if isinstance(value, datetime):
        return value
    if hasattr(value, "to_pydatetime"):
        return value.to_pydatetime()
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    return value


def day_start(value: Any) -> datetime:
    """Start-of-day (midnight) for the input date/datetime; pass-through for
    datetimes whose time is already set."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    return value


def day_end(value: Any) -> datetime:
    """End of day (23:59:59.999999, so sub-second bars count) for a plain
    ``date``; pass-through for
    datetimes."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, 23, 59, 59, 999999)
    return value


def iso(value: Any) -> str:
    """ISO-format a date/datetime-like object. Falls back to ``str()`` for
    anything else (used in client-id construction where type invariants
    are upstream)."""
    return value.isoformat() if hasattr(value, "isoformat") else str(value)
