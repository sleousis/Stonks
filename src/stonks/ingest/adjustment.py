"""Adjustment drift: did the vendor restate ``adj_close`` since the last fetch?

After a split or dividend a vendor rescales ``adj_close`` of every bar
before the event. Comparing the stored ``adj_close / close`` of a few
overlapping bars with the fresh one shows it (DS-01). The ensurer uses it
to refetch a whole history, and the pipeline to bring older stored bars
onto the new basis on every daily write (BE-09).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

import numpy as np


def adj_ratio(close: Any, adj: Any) -> float | None:
    """``adj / close`` when both are finite and positive, else ``None``."""
    try:
        c, a = float(close), float(adj)
    except (TypeError, ValueError):
        return None
    if not (np.isfinite(c) and np.isfinite(a)) or c <= 0 or a <= 0:
        return None
    return a / c


def adjustment_drift(
    stored: Mapping[date, float], fresh: Mapping[date, float], tolerance: float
) -> float | None:
    """The factor by which ``adj_close / close`` moved on the oldest day of
    both maps whose ratio moved by more than ``tolerance``, or ``None``
    when none did."""
    for day in sorted(stored.keys() & fresh.keys()):
        factor = fresh[day] / stored[day]
        if abs(factor - 1.0) > tolerance:
            return factor
    return None
