"""Intraday features over the bars of one session (roadmap 21.3.1).

Pure numpy functions: the strategies pass the closed bars of the current
session, oldest first, and every value at bar ``t`` reads bars up to ``t``
only.
"""

from __future__ import annotations

import numpy as np

__all__ = ["session_vwap"]


def session_vwap(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, volume: np.ndarray
) -> np.ndarray:
    """The running volume weighted average of the typical price
    ``(high + low + close) / 3`` from the session's first bar. While the
    session has traded no volume yet (quote-only feeds build bars with
    volume 0), each bar counts once, so the value is the running mean."""
    typical = (np.asarray(high, float) + np.asarray(low, float) + np.asarray(close, float)) / 3.0
    vol = np.nan_to_num(np.asarray(volume, float), nan=0.0)
    vol = np.where(vol > 0, vol, 0.0)
    if typical.size == 0:
        return typical
    cum_vol = np.cumsum(vol)
    cum_pv = np.cumsum(typical * vol)
    count = np.arange(1, typical.size + 1, dtype=float)
    mean = np.cumsum(typical) / count
    with np.errstate(invalid="ignore", divide="ignore"):
        weighted = cum_pv / cum_vol
    return np.where(cum_vol > 0, weighted, mean)
