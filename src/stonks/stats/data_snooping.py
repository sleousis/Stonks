"""Data-snooping tests over many strategies at once (P2, P3).

When a search tries many strategies, the best of them looks good by luck.
These tests ask whether the best is better than a benchmark after
accounting for every strategy tried. Each takes ``d``, a ``T x K`` matrix
of per-bar performance relative to the benchmark (a strategy's return
minus the benchmark's, or just its return against cash), higher is
better:

- :func:`reality_check`: White (2000). ``H0: max_k E[d_k] <= 0``. The
  statistic is the best mean, the null distribution comes from a
  stationary bootstrap recentred on every model's mean.
- :func:`spa`: Hansen (2005), the superior predictive ability test. The
  same null, studentized, and it recentres a model only while its mean is
  not clearly negative, so adding poor models does not dilute the test.
  Reports the lower, consistent and upper p-values. The consistent one is
  the test.
- :func:`romano_wolf`: Romano and Wolf (2005) step-down. One adjusted
  p-value per model with family-wise error control, so it names which
  models beat the benchmark, not only whether any did.

All three share one stationary bootstrap (Politis and Romano 1994). The
bootstrap means come from resample counts times ``d``, one matrix product,
so thousands of models and resamples stay fast. ``indices`` replaces the
seeded resamples with given ones, for exact tests.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from stonks.stats.bootstrap import stationary_bootstrap_indices

__all__ = [
    "RealityCheckResult",
    "RomanoWolfResult",
    "SPAResult",
    "bootstrap_means",
    "reality_check",
    "romano_wolf",
    "spa",
]


@dataclass(frozen=True)
class RealityCheckResult:
    #: ``sqrt(T) * max_k mean(d_k)``.
    statistic: float
    p_value: float
    #: Column of the best mean.
    best: int
    n_models: int
    n_boot: int


@dataclass(frozen=True)
class SPAResult:
    #: ``max(max_k t_k, 0)``, ``t_k`` studentized unless ``studentize=False``.
    statistic: float
    p_lower: float
    p_consistent: float
    p_upper: float
    best: int
    n_models: int
    n_boot: int

    @property
    def p_value(self) -> float:
        """Hansen's consistent p-value, the one to act on."""
        return self.p_consistent


@dataclass(frozen=True)
class RomanoWolfResult:
    #: Per model, ``t_k`` (studentized unless ``studentize=False``).
    statistics: np.ndarray
    #: Per model, the step-down adjusted p-value.
    p_values: np.ndarray
    #: Per model, adjusted p-value ``<= alpha``.
    reject: np.ndarray
    alpha: float
    n_boot: int

    @property
    def n_rejected(self) -> int:
        return int(self.reject.sum())


def bootstrap_means(
    d: np.ndarray,
    *,
    n_boot: int = 1000,
    mean_block: float = 10.0,
    seed: int | None = 0,
    indices: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """``(means, boot_means)``: the ``K`` column means of ``d`` and their
    ``B x K`` stationary-bootstrap resample means. ``indices`` (``B x T``
    row positions) replaces the seeded resamples."""
    x = _check(d)
    t = x.shape[0]
    if indices is None:
        indices = stationary_bootstrap_indices(t, mean_block, n_boot, np.random.default_rng(seed))
    idx = np.asarray(indices, dtype=np.int64)
    if idx.ndim != 2 or idx.shape[1] != t:
        raise ValueError(f"indices must be B x {t}, got shape {idx.shape}")
    if idx.size and (idx.min() < 0 or idx.max() >= t):
        raise ValueError("indices must lie in [0, T)")
    b = idx.shape[0]
    rows = np.repeat(np.arange(b, dtype=np.int64), t)
    counts = np.bincount(rows * t + idx.ravel(), minlength=b * t).reshape(b, t)
    return x.mean(axis=0), (counts @ x) / t


def reality_check(
    d: np.ndarray,
    *,
    n_boot: int = 1000,
    mean_block: float = 10.0,
    seed: int | None = 0,
    indices: np.ndarray | None = None,
) -> RealityCheckResult:
    """White's Reality Check. ``p = share of resamples with
    max_k sqrt(T)(mean*_k - mean_k) >= sqrt(T) max_k mean_k``."""
    means, boot = bootstrap_means(
        d, n_boot=n_boot, mean_block=mean_block, seed=seed, indices=indices
    )
    root_t = math.sqrt(np.asarray(d).shape[0])
    stat = root_t * float(means.max())
    null = root_t * (boot - means).max(axis=1)
    return RealityCheckResult(
        statistic=stat,
        p_value=float(np.mean(null >= stat)),
        best=int(np.argmax(means)),
        n_models=means.size,
        n_boot=boot.shape[0],
    )


def spa(
    d: np.ndarray,
    *,
    studentize: bool = True,
    n_boot: int = 1000,
    mean_block: float = 10.0,
    seed: int | None = 0,
    indices: np.ndarray | None = None,
) -> SPAResult:
    """Hansen's SPA test. Each model's deviation ``sqrt(T)(mean*_k -
    mean_k + mu_k) / omega_k`` is recentred by ``mu_k``: ``0`` (upper,
    White's recentring), ``min(mean_k, 0)`` (lower), or ``mean_k`` only
    when ``t_k <= -sqrt(2 log log T)`` (consistent). ``omega_k`` is the
    bootstrap standard deviation of ``sqrt(T) mean_k`` (``1`` without
    studentizing)."""
    means, boot = bootstrap_means(
        d, n_boot=n_boot, mean_block=mean_block, seed=seed, indices=indices
    )
    t = np.asarray(d).shape[0]
    root_t = math.sqrt(t)
    omega = _omega(means, boot, t) if studentize else np.ones_like(means)
    stats = _ratio(root_t * means, omega)
    stat = max(float(stats.max()), 0.0)
    cut = -math.sqrt(2.0 * math.log(math.log(t))) if t > 2 else -math.inf
    recentres = {
        "lower": np.minimum(means, 0.0),
        "consistent": np.where(stats <= cut, means, 0.0),
        "upper": np.zeros_like(means),
    }
    p: dict[str, float] = {}
    for name, mu in recentres.items():
        z = _ratio(root_t * (boot - means + mu), omega)
        null = np.maximum(z.max(axis=1), 0.0)
        p[name] = float(np.mean(null >= stat))
    return SPAResult(
        statistic=stat,
        p_lower=p["lower"],
        p_consistent=p["consistent"],
        p_upper=p["upper"],
        best=int(np.argmax(stats)),
        n_models=means.size,
        n_boot=boot.shape[0],
    )


def romano_wolf(
    d: np.ndarray,
    alpha: float = 0.05,
    *,
    studentize: bool = True,
    n_boot: int = 1000,
    mean_block: float = 10.0,
    seed: int | None = 0,
    indices: np.ndarray | None = None,
) -> RomanoWolfResult:
    """Romano-Wolf step-down adjusted p-values (``H0_k: E[d_k] <= 0``).

    Sort the statistics from largest to smallest. The ``j``-th model's
    p-value is the share of resamples where the largest recentred
    statistic among models ``j..K`` reaches its statistic, made monotone
    down the order. Models with an adjusted p-value ``<= alpha`` are
    rejected with the family-wise error rate held at ``alpha``."""
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must lie in (0, 1), got {alpha}")
    means, boot = bootstrap_means(
        d, n_boot=n_boot, mean_block=mean_block, seed=seed, indices=indices
    )
    t = np.asarray(d).shape[0]
    root_t = math.sqrt(t)
    omega = _omega(means, boot, t) if studentize else np.ones_like(means)
    stats = _ratio(root_t * means, omega)
    null = _ratio(root_t * (boot - means), omega)
    order = np.argsort(-stats, kind="stable")
    tail_max = np.maximum.accumulate(null[:, order][:, ::-1], axis=1)[:, ::-1]
    sorted_p = np.maximum.accumulate((tail_max >= stats[order]).mean(axis=0))
    p_values = np.empty_like(sorted_p)
    p_values[order] = sorted_p
    return RomanoWolfResult(
        statistics=stats,
        p_values=p_values,
        reject=p_values <= alpha,
        alpha=alpha,
        n_boot=boot.shape[0],
    )


def _check(d: np.ndarray) -> np.ndarray:
    x = np.asarray(d, dtype=float)
    if x.ndim == 1:
        x = x[:, None]
    if x.ndim != 2:
        raise ValueError(f"d must be T x K, got shape {x.shape}")
    if x.shape[0] < 3 or x.shape[1] < 1:
        raise ValueError(f"need at least 3 bars and 1 model, got shape {x.shape}")
    if not np.all(np.isfinite(x)):
        raise ValueError("d must be finite (fill or drop missing bars first)")
    return x


def _omega(means: np.ndarray, boot: np.ndarray, t: int) -> np.ndarray:
    """Bootstrap standard deviation of ``sqrt(T) * mean_k``."""
    return np.sqrt(t * np.mean((boot - means) ** 2, axis=0))


def _ratio(num: np.ndarray, den: np.ndarray) -> np.ndarray:
    """``num / den`` with ``0`` where ``den`` is 0 (a constant model)."""
    return np.divide(num, den, out=np.zeros(np.broadcast(num, den).shape), where=den > 0)
