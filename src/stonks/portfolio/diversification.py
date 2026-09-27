"""Diversification diagnostics (BL-44, principle P33: count bets, not
positions).

- :func:`risk_contributions`: each name's share of portfolio variance,
  ``w_i (Σw)_i / w'Σw``.
- :func:`effective_number_of_bets`: Meucci's ENB over principal
  components. With ``Σ = E Λ E'`` the book's exposure to component ``k`` is
  ``(E'w)_k``, its variance share ``p_k = (E'w)_k**2 λ_k / w'Σw`` and
  ``ENB = exp(-Σ p_k ln p_k)``. It is ``N`` for ``N`` equal uncorrelated
  bets and 1 when every name loads on one factor.
"""

from __future__ import annotations

import math

import numpy as np


def _check(weights: np.ndarray, cov: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    w = np.asarray(weights, dtype=float).ravel()
    c = np.atleast_2d(np.asarray(cov, dtype=float))
    if c.shape != (w.size, w.size):
        raise ValueError(f"weights of length {w.size} don't match a {c.shape} covariance")
    return w, 0.5 * (c + c.T)


def risk_contributions(weights: np.ndarray, cov: np.ndarray) -> np.ndarray:
    """Fraction of portfolio variance from each name (sums to 1). All zeros
    when the book has no variance."""
    w, c = _check(weights, cov)
    variance = float(w @ c @ w)
    if variance <= 0:
        return np.zeros_like(w)
    return w * (c @ w) / variance


def effective_number_of_bets(weights: np.ndarray, cov: np.ndarray) -> float:
    """Meucci's effective number of bets (0.0 for a book with no variance)."""
    w, c = _check(weights, cov)
    values, vectors = np.linalg.eigh(c)
    exposures = vectors.T @ w
    contributions = exposures**2 * np.clip(values, 0.0, None)
    total = contributions.sum()
    if total <= 0 or not math.isfinite(total):
        return 0.0
    p = contributions / total
    p = p[p > 1e-15]
    return float(math.exp(-(p * np.log(p)).sum()))
