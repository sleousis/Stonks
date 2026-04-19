"""A small toolkit of reusable feature-construction helpers.

**Not** a pipeline stage. Strategies own their own feature extraction. These
are just conveniences — any strategy is free to ignore them and compute
features inline.
"""

from __future__ import annotations

import pandas as pd


def ttm(series: pd.Series) -> pd.Series:
    """Trailing twelve months — 4-period rolling sum (assumes quarterly data)."""
    return series.rolling(window=4, min_periods=4).sum()


def pct_change_n_days(series: pd.Series, n: int) -> pd.Series:
    """N-step percentage change. Same as ``series.pct_change(n)`` but explicit."""
    return series.pct_change(periods=n)


def rolling_zscore(series: pd.Series, window: int) -> pd.Series:
    """Rolling (x - mean) / std with the given window."""
    mean = series.rolling(window=window, min_periods=window).mean()
    std = series.rolling(window=window, min_periods=window).std()
    return (series - mean) / std.replace(0, pd.NA)


def trailing_return(prices: pd.Series, n_days: int) -> pd.Series:
    """Return of ``prices`` over the last ``n_days`` steps. Same semantics as
    ``pct_change(n_days)`` but named for its intended use in momentum-style
    features.
    """
    return prices.pct_change(periods=n_days)


def rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index using Wilder's smoothing.

    Implementation equivalent to the canonical formula used by
    ``pandas_ta.rsi`` (EWM with ``alpha = 1/period``, ``adjust=False``),
    without pulling in the extra dependency. The first ``period`` outputs
    are ``NaN`` because Wilder's average needs that many gain/loss
    observations to warm up.
    """
    delta = prices.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)
