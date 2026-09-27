"""Hierarchical risk parity (Lopez de Prado 2016; BL-44).

1. Distance between names from their correlation, ``d = sqrt((1 - rho) / 2)``.
2. Cluster the names (single linkage by default) and put them in the tree's
   leaf order, so similar names sit next to each other.
3. Recursive bisection: split the ordered list in halves, give each half
   weight in inverse proportion to its variance (each half held at
   inverse-variance weights), and repeat inside each half.

HRP never inverts the covariance, so it copes with near-singular matrices.
On a diagonal covariance it gives inverse-variance weights.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
from scipy.cluster.hierarchy import leaves_list, linkage
from scipy.spatial.distance import squareform

from stonks.portfolio._risk_based import CovarianceView, RiskBasedConstructor, RiskBasedSettings
from stonks.portfolio.base import register_constructor

Linkage = Literal["single", "average", "complete", "ward"]
_LINKAGES = ("single", "average", "complete", "ward")


def _inverse_variance(cov: np.ndarray) -> np.ndarray:
    inv = 1.0 / np.clip(np.diag(cov), 1e-300, None)
    return inv / inv.sum()


def _cluster_variance(cov: np.ndarray, items: list[int]) -> float:
    sub = cov[np.ix_(items, items)]
    w = _inverse_variance(sub)
    return float(w @ sub @ w)


def hrp_weights(cov: np.ndarray, linkage_method: str = "single") -> np.ndarray:
    """HRP weights (summing to 1) for a covariance matrix."""
    if linkage_method not in _LINKAGES:
        raise ValueError(f"unknown linkage {linkage_method!r}; choose one of {_LINKAGES}")
    c = np.atleast_2d(np.asarray(cov, dtype=float))
    n = c.shape[0]
    if n == 1:
        return np.ones(1)
    std = np.sqrt(np.clip(np.diag(c), 1e-300, None))
    corr = np.clip(c / np.outer(std, std), -1.0, 1.0)
    dist = np.sqrt(np.clip((1.0 - corr) / 2.0, 0.0, None))
    np.fill_diagonal(dist, 0.0)
    order = leaves_list(linkage(squareform(dist, checks=False), method=linkage_method)).tolist()
    weights = np.ones(n)
    stack = [order]
    while stack:
        items = stack.pop()
        if len(items) < 2:
            continue
        half = len(items) // 2
        left, right = items[:half], items[half:]
        var_left, var_right = _cluster_variance(c, left), _cluster_variance(c, right)
        total = var_left + var_right
        alpha = 0.5 if total <= 0 else 1.0 - var_left / total
        weights[left] *= alpha
        weights[right] *= 1.0 - alpha
        stack.extend([left, right])
    return weights / weights.sum()


class HrpSettings(RiskBasedSettings):
    linkage: Linkage = "single"


@register_constructor("hrp")
class HierarchicalRiskParity(RiskBasedConstructor):
    """HRP over the ``top_n`` best positive combined scores. Scores pick
    the names; the covariance alone sizes them."""

    Settings = HrpSettings

    def raw_weights(self, cov: CovarianceView, scores: np.ndarray) -> np.ndarray:
        return hrp_weights(cov.matrix, self.settings.linkage)  # type: ignore[attr-defined]
