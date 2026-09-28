"""Newey & West (1987) heteroskedasticity- and autocorrelation-consistent SE,
and the HAC t-test of a mean built on it."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.stats import norm


def default_lags(t: int) -> int:
    """Newey-West's rule of thumb: ``floor(4 * (t/100)^(2/9))``."""
    return int(math.floor(4 * (t / 100) ** (2 / 9)))


def newey_west_se(x: np.ndarray, lags: int | None = None) -> float:
    """Standard error of ``mean(x)`` with Bartlett-weighted autocovariances:
    ``sqrt((g0 + 2*sum_{l=1..L} (1 - l/(L+1)) * g_l) / T)`` where ``g_l`` is
    the lag-``l`` autocovariance with divisor ``T``. With ``lags=0`` this is
    the iid ``std(ddof=0) / sqrt(T)``. NaNs are dropped."""
    v = np.asarray(x, dtype=float)
    v = v[np.isfinite(v)]
    t = v.size
    if t < 2:
        raise ValueError(f"need at least 2 observations, got {t}")
    n_lags = default_lags(t) if lags is None else int(lags)
    if n_lags < 0:
        raise ValueError(f"lags must be non-negative, got {lags}")
    n_lags = min(n_lags, t - 1)
    d = v - v.mean()
    s = float(d @ d) / t
    for lag in range(1, n_lags + 1):
        s += 2 * (1 - lag / (n_lags + 1)) * float(d[lag:] @ d[:-lag]) / t
    return math.sqrt(max(s, 0.0) / t)


@dataclass(frozen=True)
class MeanTest:
    """A t-test of ``mean(x) = 0`` with a Newey-West standard error."""

    n: int
    mean: float
    se: float
    t_stat: float
    #: One-sided p-value against ``mean < 0``: ``Phi(t)``.
    p_below: float


def hac_mean_test(x: np.ndarray, lags: int | None = None) -> MeanTest:
    """HAC t-test of the mean of ``x`` (for a paired test pass the per-bar
    differences). With a zero standard error the statistic is 0 for a zero
    mean and infinite otherwise. NaNs are dropped."""
    v = np.asarray(x, dtype=float)
    v = v[np.isfinite(v)]
    se = newey_west_se(v, lags)
    mean = float(v.mean())
    flat = 0.0 if mean == 0 else math.copysign(math.inf, mean)
    t = mean / se if se > 0 else flat
    return MeanTest(n=int(v.size), mean=mean, se=se, t_stat=t, p_below=float(norm.cdf(t)))
