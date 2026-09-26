"""Stationary bootstrap (Politis & Romano 1994) and a Sharpe confidence interval."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


def stationary_bootstrap_indices(
    t: int, mean_block: float, n: int, rng: np.random.Generator
) -> np.ndarray:
    """``n`` resamples of ``range(t)`` as an ``(n, t)`` index array.

    Each resample is a chain of blocks with geometric lengths of mean
    ``mean_block`` that start at uniform positions and wrap around the end
    of the series, so the resampled series stays stationary."""
    if t <= 0 or n <= 0:
        raise ValueError(f"t and n must be positive, got t={t}, n={n}")
    if mean_block < 1:
        raise ValueError(f"mean_block must be >= 1, got {mean_block}")
    starts = rng.integers(0, t, size=(n, t))
    new_block = rng.random((n, t)) < 1.0 / mean_block
    new_block[:, 0] = True
    pos = np.arange(t)
    # Column where the current block began, carried forward.
    block_start = np.maximum.accumulate(np.where(new_block, pos, 0), axis=1)
    first = np.take_along_axis(starts, block_start, axis=1)
    return (first + (pos - block_start)) % t


@dataclass(frozen=True)
class SharpeCI:
    sharpe: float  # per bar, ddof=1
    lower: float
    upper: float


def sharpe_ci(
    returns: np.ndarray,
    alpha: float = 0.05,
    n_boot: int = 2000,
    mean_block: float = 20,
    seed: int | None = 0,
) -> SharpeCI:
    """Percentile ``1 - alpha`` interval for the per-bar Sharpe ratio from a
    seeded stationary bootstrap. NaNs are dropped."""
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if r.size < 3:
        raise ValueError(f"need at least 3 returns, got {r.size}")
    idx = stationary_bootstrap_indices(r.size, mean_block, n_boot, np.random.default_rng(seed))
    sample = r[idx]
    sd = sample.std(axis=1, ddof=1)
    boot = np.divide(sample.mean(axis=1), sd, out=np.zeros(n_boot), where=sd > 0)
    lo, hi = np.quantile(boot, [alpha / 2, 1 - alpha / 2])
    point_sd = r.std(ddof=1)
    point = float(r.mean() / point_sd) if point_sd > 0 else 0.0
    return SharpeCI(sharpe=point, lower=float(lo), upper=float(hi))
