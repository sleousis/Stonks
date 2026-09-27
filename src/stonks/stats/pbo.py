"""Probability of backtest overfitting via combinatorially symmetric
cross-validation (Bailey, Borwein, Lopez de Prado & Zhu 2015).

The ``T x N`` matrix ``M`` holds per-bar returns of ``N`` trials. Rows are
cut into ``S`` contiguous blocks; for every split of the blocks into two
halves, the trial with the best in-sample Sharpe is located in the
out-of-sample ranking. ``omega = rank / (N + 1)`` and
``lambda = ln(omega / (1 - omega))``; PBO is the share of splits with
``lambda <= 0`` (the in-sample winner lands at or below the out-of-sample
median).
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass

import numpy as np
from scipy.stats import rankdata


@dataclass(frozen=True)
class PBOResult:
    pbo: float
    #: OLS slope of the winner's OOS Sharpe on its IS Sharpe; below 1 means
    #: performance degrades out of sample.
    degradation_slope: float
    #: Share of splits where the in-sample winner loses money out of sample.
    p_loss: float
    #: Splits that were scored (degenerate ones are skipped, see ``cscv``).
    n_combinations: int


def cscv(
    m: np.ndarray,
    n_blocks: int = 10,
    max_combinations: int = 5000,
    seed: int | None = 0,
) -> PBOResult:
    """CSCV over ``m`` with ``n_blocks`` (even) blocks. When
    ``C(n_blocks, n_blocks/2)`` exceeds ``max_combinations``, that many
    distinct splits are sampled with ``seed``. NaNs (failed bars or trials)
    are ignored per column; a trial with no usable data in a half never
    wins it and ranks last. A split where no trial has a usable Sharpe in
    one half has no winner (or no ranking) and is skipped, so it counts
    neither as overfit nor as not. With no usable split every rate is NaN."""
    x = np.asarray(m, dtype=float)
    if x.ndim != 2:
        raise ValueError(f"m must be a 2-D T x N matrix, got shape {x.shape}")
    t, n = x.shape
    if n_blocks < 2 or n_blocks % 2:
        raise ValueError(f"n_blocks must be an even number >= 2, got {n_blocks}")
    if n < 2:
        raise ValueError(f"need at least 2 trials, got {n}")
    if t < 2 * n_blocks:
        raise ValueError(f"need at least {2 * n_blocks} bars for {n_blocks} blocks, got {t}")

    # Per-block sufficient statistics, so each split is a cheap sum.
    finite = np.isfinite(x)
    vals = np.where(finite, x, 0.0)
    blocks = np.array_split(np.arange(t), n_blocks)
    cnt = np.stack([finite[b].sum(axis=0) for b in blocks])  # (S, N)
    s1 = np.stack([vals[b].sum(axis=0) for b in blocks])
    s2 = np.stack([(vals[b] ** 2).sum(axis=0) for b in blocks])

    combos = _combinations(n_blocks, max_combinations, seed)
    is_sr: list[float] = []
    oos_sr: list[float] = []
    lam: list[float] = []
    all_blocks = np.arange(n_blocks)
    for train in combos:
        test = np.setdiff1d(all_blocks, train)
        perf_is = _sharpe(cnt[train].sum(0), s1[train].sum(0), s2[train].sum(0))
        perf_oos = _sharpe(cnt[test].sum(0), s1[test].sum(0), s2[test].sum(0))
        if not (np.isfinite(perf_is).any() and np.isfinite(perf_oos).any()):
            continue  # no in-sample winner or no out-of-sample ranking
        best = int(np.argmax(perf_is))
        omega = rankdata(perf_oos)[best] / (n + 1)
        lam.append(math.log(omega / (1 - omega)))
        is_sr.append(float(perf_is[best]))
        oos_sr.append(float(perf_oos[best]))

    if not lam:
        nan = float("nan")
        return PBOResult(pbo=nan, degradation_slope=nan, p_loss=nan, n_combinations=0)
    oos = np.asarray(oos_sr)
    return PBOResult(
        pbo=float(np.mean(np.asarray(lam) <= 0)),
        degradation_slope=_slope(np.asarray(is_sr), oos),
        p_loss=float(np.mean(oos < 0)),
        n_combinations=len(lam),
    )


def _sharpe(cnt: np.ndarray, s1: np.ndarray, s2: np.ndarray) -> np.ndarray:
    """Per-column Sharpe (ddof=1) from sufficient statistics; columns with
    fewer than 2 bars or no variance score ``-inf`` (they never win)."""
    with np.errstate(divide="ignore", invalid="ignore"):
        mean = s1 / cnt
        var = (s2 - cnt * mean**2) / (cnt - 1)
        out = mean / np.sqrt(var)
    ok = (cnt > 1) & (var > 1e-300)
    return np.where(ok, out, -np.inf)


def _slope(x: np.ndarray, y: np.ndarray) -> float:
    ok = np.isfinite(x) & np.isfinite(y)
    x, y = x[ok], y[ok]
    if x.size < 2 or float(np.var(x)) == 0.0:
        return float("nan")
    return float(np.polyfit(x, y, 1)[0])


def _combinations(s: int, cap: int, seed: int | None) -> list[np.ndarray]:
    half = s // 2
    total = math.comb(s, half)
    if total <= cap:
        return [np.array(c) for c in itertools.combinations(range(s), half)]
    rng = np.random.default_rng(seed)
    seen: set[tuple[int, ...]] = set()
    out: list[np.ndarray] = []
    while len(out) < cap:
        c = tuple(sorted(rng.choice(s, size=half, replace=False).tolist()))
        if c not in seen:
            seen.add(c)
            out.append(np.array(c))
    return out
