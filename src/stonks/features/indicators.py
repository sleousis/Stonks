"""Shared volatility indicators used by several strategies.

``atr`` follows the two conventions found in the ports of the
neurotrader888 research code: Wilder/RMA smoothing (``pandas_ta.atr``'s
default) and a simple rolling mean of true range. Both are causal: the
value at bar ``i`` only uses bars ``<= i``.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd

AtrMethod = Literal["rma", "sma"]


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """Per-bar true range. Undefined (NaN) on the first bar, which has no
    previous close."""
    prev_close = close.shift(1)
    ranges = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1)
    tr = ranges.max(axis=1, skipna=False)
    return tr.astype(float)


def atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    lookback: int,
    method: AtrMethod = "rma",
) -> pd.Series:
    """Average true range over ``lookback`` bars.

    ``rma`` is Wilder smoothing (``ewm(alpha=1/lookback)``) and ``sma`` is a
    plain rolling mean. The first ``lookback`` bars are NaN in both cases.
    """
    if lookback < 1:
        raise ValueError(f"lookback must be >= 1, got {lookback}")
    tr = true_range(high, low, close)
    if method == "rma":
        return tr.ewm(alpha=1.0 / lookback, min_periods=lookback).mean()
    if method == "sma":
        return tr.rolling(lookback).mean()
    raise ValueError(f"method must be 'rma' or 'sma', got {method!r}")


def efficiency_ratio(close: pd.Series, period: int = 10) -> pd.Series:
    """Kaufman's efficiency ratio: net move over ``period`` bars divided by
    the sum of the bar-to-bar moves, in ``[0, 1]``. 1 on a straight line,
    near 0 in chop. NaN for the first ``period`` bars and on a flat window."""
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")
    close = close.astype(float)
    change = (close - close.shift(period)).abs()
    path = close.diff().abs().rolling(period, min_periods=period).sum()
    return (change / path.where(path > 0)).astype(float)


def kama(close: pd.Series, period: int = 10, fast: int = 2, slow: int = 30) -> pd.Series:
    """Kaufman's adaptive moving average. The smoothing constant is
    ``(ER * (2/(fast+1) - 2/(slow+1)) + 2/(slow+1))^2``, so KAMA tracks
    close closely in a clean trend and barely moves in chop. Seeded with
    the close on the first bar that has an ER; NaN before it."""
    if not 1 <= fast < slow:
        raise ValueError(f"need 1 <= fast < slow, got fast={fast}, slow={slow}")
    close = close.astype(float)
    fast_sc, slow_sc = 2.0 / (fast + 1), 2.0 / (slow + 1)
    er = efficiency_ratio(close, period).fillna(0.0).to_numpy()
    values = close.to_numpy()
    out = np.full(len(values), np.nan)
    for i in range(period, len(values)):
        sc = (er[i] * (fast_sc - slow_sc) + slow_sc) ** 2
        prev = out[i - 1] if i > period else values[i - 1]
        out[i] = prev + sc * (values[i] - prev)
    return pd.Series(out, index=close.index)
