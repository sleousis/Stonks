"""Family-wise (Holm 1979) and false-discovery (Benjamini-Hochberg 1995) control.

Both return a boolean reject mask in the input order."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np


def holm(pvals: Sequence[float], alpha: float = 0.05) -> np.ndarray:
    """Holm's step-down: walk the sorted p-values and reject while
    ``p_(k) <= alpha / (m - k + 1)``; stop at the first failure."""
    p = np.asarray(pvals, dtype=float)
    m = p.size
    reject = np.zeros(m, dtype=bool)
    for rank, i in enumerate(np.argsort(p, kind="stable")):
        if p[i] > alpha / (m - rank):
            break
        reject[i] = True
    return reject


def benjamini_hochberg(pvals: Sequence[float], q: float = 0.05) -> np.ndarray:
    """Benjamini-Hochberg step-up: find the largest ``k`` with
    ``p_(k) <= k*q/m`` and reject the ``k`` smallest p-values."""
    p = np.asarray(pvals, dtype=float)
    m = p.size
    reject = np.zeros(m, dtype=bool)
    if m == 0:
        return reject
    order = np.argsort(p, kind="stable")
    passing = np.nonzero(p[order] <= np.arange(1, m + 1) * q / m)[0]
    if passing.size:
        reject[order[: passing[-1] + 1]] = True
    return reject
