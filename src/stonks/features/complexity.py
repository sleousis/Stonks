"""Rolling complexity and time-reversibility features.

Ported (own implementation, no code copied) from neurotrader888's MIT
licensed research repos:

- ``PermutationEntropy`` (``perm_entropy.py``) -> :func:`rolling_permutation_entropy`
- ``TimeSeriesReversibility`` (``reversibility.py`` / ``indicators.py``) ->
  :func:`rolling_ptsr`, :func:`relative_async_index`, :func:`rolling_rai`
- ``TradeDependenceRunsTest`` (``runs_indicator.py``) -> :func:`rolling_runs_z`

Every rolling function is causal: the value at index ``i`` only reads
``arr[: i + 1]``.

Deviations from the originals:

- ``window`` always counts raw samples. The original PTSR indicator used
  ``lookback + 2`` samples for a ``lookback`` argument; pass
  ``window = lookback + 2`` to reproduce it.
- :func:`rolling_permutation_entropy` wraps
  :func:`stonks.features.library.permutation_entropy`, whose window is
  ``d! * lookback_mult``; ``window`` is rounded to the nearest multiple of
  ``d!`` (at least ``d!``).
- :func:`rolling_ptsr` supports any ordinal-pattern length ``d`` (the
  original fixed ``d = 3``) and offers the library's Laplace-smoothed
  estimate as ``missing="laplace"`` besides the original "NaN and carry the
  previous value" mode (the default).
- The horizontal visibility graph is built with an O(n) stack instead of
  the ``ts2vg`` package; links follow the strict criterion (every sample
  between two nodes strictly below both).
- :func:`async_index` sorts with a stable sort so ties are deterministic.
- :func:`relative_async_index` returns NaN (instead of ``inf`` / a 0/0
  warning) when either asynchronous index is zero.
- :func:`rolling_runs_z` returns NaN for windows with no up or no down move
  (the original divided by zero).
"""

from __future__ import annotations

import math
from typing import Literal

import numpy as np
import pandas as pd

from stonks.features.library import (
    ordinal_patterns,
    perm_ts_reversibility,
    permutation_entropy,
    runs_test_z_score,
)

PtsrMissing = Literal["carry", "laplace"]

__all__ = [
    "async_index",
    "hvg_out_degrees",
    "ptsr",
    "relative_async_index",
    "rolling_permutation_entropy",
    "rolling_ptsr",
    "rolling_rai",
    "rolling_runs_z",
]


# ---- permutation entropy -------------------------------------------------------


def rolling_permutation_entropy(arr: np.ndarray, window: int, d: int = 3) -> np.ndarray:
    """Normalized permutation entropy over a rolling window of ordinal
    patterns (``window`` rounded to a multiple of ``d!``). Low values mean
    strong structure; values near 1 mean noise."""
    fac = math.factorial(d)
    mult = max(1, round(window / fac))
    return permutation_entropy(np.asarray(arr, dtype=float), d=d, lookback_mult=mult)


# ---- permutation time-series reversibility ------------------------------------


def ptsr(window: np.ndarray, d: int = 3) -> float:
    """Zanin et al. permutation time-series reversibility of one window: the
    KL divergence between the ordinal-pattern distribution of the window and
    of its time reversal. NaN when any pattern is missing in either
    direction (the divergence is then undefined or infinite)."""
    window = np.asarray(window, dtype=float)
    fac = math.factorial(d)
    fwd = ordinal_patterns(window, d)[d - 1 :].astype(int)
    rev = ordinal_patterns(window[::-1], d)[d - 1 :].astype(int)
    n = len(fwd)
    if n == 0:
        return float("nan")
    p_f = np.bincount(fwd, minlength=fac) / n
    p_r = np.bincount(rev, minlength=fac) / n
    if p_f.min() <= 0.0 or p_r.min() <= 0.0:
        return float("nan")
    return float(np.sum(p_f * np.log(p_f / p_r)))


def rolling_ptsr(
    arr: np.ndarray, window: int, d: int = 3, missing: PtsrMissing = "carry"
) -> np.ndarray:
    """Rolling :func:`ptsr` over ``window`` samples.

    ``missing="carry"`` (the original): a window with a missing pattern
    repeats the previous value. ``missing="laplace"``: every window uses
    the library's add-one smoothed estimate, which is always finite.
    """
    if missing not in ("carry", "laplace"):
        raise ValueError(f"missing must be 'carry' or 'laplace', got {missing!r}")
    if window < max(10, d + 1):
        raise ValueError(f"window must be >= {max(10, d + 1)}, got {window}")
    arr = np.asarray(arr, dtype=float)
    out = np.full(len(arr), np.nan, dtype=float)
    for i in range(window - 1, len(arr)):
        dat = arr[i - window + 1 : i + 1]
        if missing == "laplace":
            out[i] = perm_ts_reversibility(dat, d=d)
            continue
        value = ptsr(dat, d)
        out[i] = out[i - 1] if np.isnan(value) and i > 0 else value
    return out


# ---- horizontal visibility graph + relative asynchronous index ----------------


def hvg_out_degrees(arr: np.ndarray) -> np.ndarray:
    """Out-degree (links to later samples) of every node of the horizontal
    visibility graph of ``arr``: ``i < j`` are linked when every sample
    strictly between them is strictly below ``min(arr[i], arr[j])``."""
    arr = np.asarray(arr, dtype=float)
    out = np.zeros(len(arr), dtype=int)
    stack: list[int] = []
    for j, value in enumerate(arr):
        # every lower sample still visible sees j, then is hidden behind it
        while stack and arr[stack[-1]] < value:
            out[stack.pop()] += 1
        if stack:
            out[stack[-1]] += 1
            if arr[stack[-1]] == value:
                stack.pop()  # an equal height hides it from everything later
        stack.append(j)
    return out


def async_index(a: np.ndarray, b: np.ndarray) -> float:
    """Asynchronous index AI(a, b): the fraction of pairs, ordered by ``a``,
    whose ``b`` values are inverted. Not symmetric in general."""
    a = np.asarray(a)
    b = np.asarray(b)
    if len(a) != len(b):
        raise ValueError("a and b must have the same length")
    n = len(a)
    if n < 2:
        return float("nan")
    ordered = b[np.argsort(a, kind="stable")].astype(float)
    inversions = int(np.sum(np.triu(ordered[:, None] > ordered[None, :], k=1)))
    return inversions / (n * (n - 1) / 2.0)


def relative_async_index(arr: np.ndarray) -> float:
    """Relative asynchronous index of one window:
    ``-ln(min(AI_fr, AI_rf) / max(AI_fr, AI_rf))`` over the visibility-graph
    out-degrees of the window and of its time reversal. Near 0 for
    reversible series; larger for irreversible dynamics."""
    arr = np.asarray(arr, dtype=float)
    out_f = hvg_out_degrees(arr)
    out_r = hvg_out_degrees(arr[::-1])
    fr = async_index(out_f, out_r)
    rf = async_index(out_r, out_f)
    lo, hi = min(fr, rf), max(fr, rf)
    if not lo > 0.0:  # zero or NaN
        return float("nan")
    return float(-math.log(lo / hi))


def rolling_rai(arr: np.ndarray, window: int, smooth_com: float | None = None) -> np.ndarray:
    """Rolling :func:`relative_async_index` over ``window`` samples,
    optionally smoothed with ``ewm(com=smooth_com)`` (the original used 7)."""
    if window < 3:
        raise ValueError(f"window must be >= 3, got {window}")
    arr = np.asarray(arr, dtype=float)
    out = np.full(len(arr), np.nan, dtype=float)
    for i in range(window - 1, len(arr)):
        out[i] = relative_async_index(arr[i - window + 1 : i + 1])
    if smooth_com is not None:
        out = pd.Series(out).ewm(com=smooth_com).mean().to_numpy()
    return out


# ---- rolling runs test --------------------------------------------------------


def rolling_runs_z(close: pd.Series | np.ndarray, lookback: int) -> np.ndarray:
    """Wald-Wolfowitz runs z-score of the signs of ``close.diff()`` over the
    last ``lookback`` changes. Negative: moves cluster (trend); positive:
    moves alternate (mean reversion)."""
    if lookback < 2:
        raise ValueError(f"lookback must be >= 2, got {lookback}")
    signs = np.sign(np.diff(np.asarray(close, dtype=float), prepend=np.nan))
    out = np.full(len(signs), np.nan, dtype=float)
    for i in range(lookback, len(signs)):
        out[i] = runs_test_z_score(signs[i - lookback + 1 : i + 1])
    return out
