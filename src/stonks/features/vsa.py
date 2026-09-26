"""Volume Spread Analysis (VSA) range/volume deviation.

Source: neurotrader888/VSAIndicator, ``vsa.py`` (MIT License, (c) 2023
neurotrader888). Stonks' own implementation of the algorithm; no code is
copied.

Algorithm: normalise each bar's range by the ATR (``norm_range = (H - L) /
ATR_rma(n)``) and its volume by the rolling median (``norm_volume = V /
median(V, n)``), regress ``norm_range`` on ``norm_volume`` over a trailing
window of ``n`` bars, and report how far the bar's actual normalised range
sits from the fitted one. When the fit shows no sensible positive relation
(``slope <= 0`` or ``r < 0.2``) the deviation is 0. A strongly negative
value is a bar whose range is small for its volume (absorption); a strongly
positive one is a wide bar on little volume.

Deliberate deviations from the original:

- The regression for bar ``i`` is fitted on bars ``[i-n, i-1]``; the
  original includes bar ``i`` in its own fit, which pulls the fit towards
  the bar being judged and shrinks every deviation. Excluding it makes the
  deviation a genuine out-of-sample residual.
- The rolling regression uses rolling sums (vectorised) instead of
  ``scipy.stats.linregress`` per bar; values are identical up to float
  rounding.
- The first valid value is at bar ``2n`` (the ATR, median and a full
  window of valid inputs are all needed); the original also starts at
  ``2n``. A zero ATR / zero median bar yields NaN rather than ``inf``; a
  window with zero volume variance yields 0 (the original yields NaN).
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from stonks.features.indicators import atr

__all__ = ["MIN_R", "ols", "range_volume_deviation"]

#: Minimum correlation for the fit to count (the original's 0.2).
MIN_R = 0.2
_EPS = 1e-12


def ols(x: np.ndarray, y: np.ndarray) -> tuple[float, float, float]:
    """Least-squares line ``y = intercept + slope * x``; returns
    ``(slope, intercept, r)``. NaNs when ``x`` or ``y`` has no variance."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    xm, ym = x.mean(), y.mean()
    sxx = float(((x - xm) ** 2).sum())
    syy = float(((y - ym) ** 2).sum())
    sxy = float(((x - xm) * (y - ym)).sum())
    if sxx <= _EPS or syy <= _EPS:
        return math.nan, math.nan, math.nan
    slope = sxy / sxx
    return slope, float(ym - slope * xm), sxy / math.sqrt(sxx * syy)


def range_volume_deviation(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
    norm_lookback: int,
) -> np.ndarray:
    """Per-bar VSA deviation (see module docstring). NaN during warm-up.
    Causal: value ``i`` depends only on bars ``<= i``."""
    n = int(norm_lookback)
    if n < 2:
        raise ValueError(f"norm_lookback must be >= 2, got {n}")
    high = pd.Series(np.asarray(high, dtype=float))
    low = pd.Series(np.asarray(low, dtype=float))
    close = pd.Series(np.asarray(close, dtype=float))
    volume = pd.Series(np.asarray(volume, dtype=float))

    atr_ = atr(high, low, close, n, method="rma")
    vol_med = volume.rolling(n).median()
    norm_range = (high - low) / atr_.where(atr_ > 0)
    norm_volume = volume / vol_med.where(vol_med > 0)

    x, y = norm_volume, norm_range
    # Rolling sums over the n bars *before* each bar (shift(1)).
    sx = x.rolling(n).sum().shift(1)
    sy = y.rolling(n).sum().shift(1)
    sxx = (x * x).rolling(n).sum().shift(1)
    syy = (y * y).rolling(n).sum().shift(1)
    sxy = (x * y).rolling(n).sum().shift(1)
    # rolling() sums of all-finite windows only; any NaN in the window -> NaN
    valid_window = (x.notna() & y.notna()).astype(float).rolling(n).sum().shift(1) == n

    var_x = n * sxx - sx * sx
    var_y = n * syy - sy * sy
    cov = n * sxy - sx * sy
    flat = (var_x <= _EPS * (n * sxx).abs()) | (var_y <= _EPS * (n * syy).abs())
    safe_var_x = var_x.where(~flat)
    slope = cov / safe_var_x
    intercept = (sy - slope * sx) / n
    r = cov / np.sqrt(safe_var_x * var_y.where(~flat))

    fitted = intercept + slope * x
    dev = (y - fitted).where((slope > 0) & (r >= MIN_R), 0.0)
    dev = dev.where(~flat, 0.0)
    ok = valid_window & x.notna() & y.notna()
    return dev.where(ok).to_numpy(dtype=float)
