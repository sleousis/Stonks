"""Market-profile support/resistance levels.

Source: neurotrader888/TechnicalAnalysisAutomation, ``mp_support_resist.py``
(MIT License, (c) 2023 neurotrader888). Stonks' own implementation of the
algorithm; no code is copied.

Algorithm: estimate where price has spent its time with a weighted Gaussian
kernel density of log closes (recent bars weigh more, weights rising
linearly from ``first_w`` to 1), evaluate it on a 200-point grid spanning the
window's range, and call every density peak whose prominence is at least
``prom_thresh`` times the highest density a level.

Deliberate deviations from the original:

- No scipy. :func:`weighted_gaussian_kde` reproduces
  ``scipy.stats.gaussian_kde(..., bw_method=factor, weights=w)``: the kernel
  standard deviation is ``factor`` times the weighted sample standard
  deviation (with scipy's effective-sample-size correction). The original
  passes ``log ATR * atr_mult`` as that factor, so the kernel width is
  ``log ATR * atr_mult * weighted std(log close)``; we keep that meaning.
- :func:`find_peaks` / :func:`peak_prominences` reimplement the subset of
  ``scipy.signal.find_peaks`` the original uses (strict local maxima,
  plateau midpoints, edges never peaks, topographic prominence).
- Degenerate windows (constant prices, non-finite or non-positive ATR)
  return no levels instead of raising.
"""

from __future__ import annotations

import math

import numpy as np

__all__ = ["find_peaks", "market_profile_levels", "peak_prominences", "weighted_gaussian_kde"]

_SQRT_2PI = math.sqrt(2.0 * math.pi)


def weighted_gaussian_kde(
    samples: np.ndarray, weights: np.ndarray, bandwidth_factor: float, grid: np.ndarray
) -> np.ndarray:
    """Weighted Gaussian KDE evaluated on ``grid`` (integrates to 1).

    Kernel sigma = ``bandwidth_factor * weighted_std(samples)`` where the
    weighted variance is ``sum w (x - m)^2 / (1 - sum w^2)`` on normalised
    weights (scipy's convention). Returns zeros when that sigma is 0.
    """
    x = np.asarray(samples, dtype=float)
    w = np.asarray(weights, dtype=float)
    grid = np.asarray(grid, dtype=float)
    total = w.sum()
    if len(x) < 2 or total <= 0:
        return np.zeros_like(grid)
    w = w / total
    mean = float(np.dot(w, x))
    denom = 1.0 - float(np.dot(w, w))
    if denom <= 0:
        return np.zeros_like(grid)
    var = float(np.dot(w, (x - mean) ** 2)) / denom
    sigma = bandwidth_factor * math.sqrt(var)
    if not sigma > 0:
        return np.zeros_like(grid)
    z = (grid[:, None] - x[None, :]) / sigma
    return (np.exp(-0.5 * z * z) @ w) / (sigma * _SQRT_2PI)


def _local_maxima(y: np.ndarray) -> np.ndarray:
    peaks: list[int] = []
    n = len(y)
    i = 1
    while i < n - 1:
        if y[i - 1] < y[i]:
            ahead = i + 1
            while ahead < n - 1 and y[ahead] == y[i]:
                ahead += 1
            if y[ahead] < y[i]:
                peaks.append((i + ahead - 1) // 2)
                i = ahead
                continue
        i += 1
    return np.asarray(peaks, dtype=int)


def peak_prominences(y: np.ndarray, peaks: np.ndarray) -> np.ndarray:
    """Topographic prominence of each peak: its height above the higher of
    the two lowest points reached before meeting higher ground (or an edge)
    on either side."""
    y = np.asarray(y, dtype=float)
    out = np.empty(len(peaks), dtype=float)
    for k, p in enumerate(peaks):
        height = y[p]
        left_min = height
        i = p
        while i >= 0 and y[i] <= height:
            left_min = min(left_min, y[i])
            i -= 1
        right_min = height
        i = p
        while i < len(y) and y[i] <= height:
            right_min = min(right_min, y[i])
            i += 1
        out[k] = height - max(left_min, right_min)
    return out


def find_peaks(y: np.ndarray, min_prominence: float | None = None) -> np.ndarray:
    """Indices of local maxima of ``y`` (flat tops report their middle,
    edges never count), optionally keeping only those with prominence
    ``>= min_prominence``."""
    y = np.asarray(y, dtype=float)
    peaks = _local_maxima(y)
    if min_prominence is None or len(peaks) == 0:
        return peaks
    return peaks[peak_prominences(y, peaks) >= min_prominence]


def market_profile_levels(
    log_prices: np.ndarray,
    log_atr: float,
    first_w: float = 0.01,
    atr_mult: float = 3.0,
    prom_thresh: float = 0.25,
    grid_points: int = 200,
) -> list[float]:
    """Support/resistance price levels (in price, not log, space), ascending.

    ``log_prices`` is the window of log closes, oldest first; ``log_atr``
    the ATR of log prices at the window's last bar.
    """
    x = np.asarray(log_prices, dtype=float)
    x = x[np.isfinite(x)]
    factor = log_atr * atr_mult
    if len(x) < 2 or not math.isfinite(factor) or factor <= 0:
        return []
    lo, hi = float(x.min()), float(x.max())
    if hi <= lo:
        return []
    n = len(x)
    weights = np.clip(first_w + np.arange(n) * (1.0 - first_w) / n, 0.0, None)
    grid = lo + np.arange(grid_points) * (hi - lo) / grid_points
    pdf = weighted_gaussian_kde(x, weights, factor, grid)
    top = float(pdf.max())
    if not top > 0:
        return []
    peaks = find_peaks(pdf, min_prominence=top * prom_thresh)
    return [float(math.exp(grid[p])) for p in peaks]
