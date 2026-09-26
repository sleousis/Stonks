"""Shared volatility indicators used by several strategies.

``atr`` follows the two conventions found in the ports of the
neurotrader888 research code: Wilder/RMA smoothing (``pandas_ta.atr``'s
default) and a simple rolling mean of true range. Both are causal: the
value at bar ``i`` only uses bars ``<= i``.
"""

from __future__ import annotations

from typing import Literal

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
