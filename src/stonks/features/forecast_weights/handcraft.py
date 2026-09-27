"""Handcrafted forecast weights (Carver, *Systematic Trading* ch. 4 and
his handcrafting method in pysystemtrade).

Rules that move together share weight. Pre-cost Sharpe ratios are not
trusted (they are too noisy to tell rules apart), but costs are, because
they are known well:

1. Cluster the rules on the correlation of their net returns (average
   linkage on ``sqrt((1 - rho) / 2)``).
2. Walk the tree from the top. At each split the two groups get weight in
   proportion to their effective number of independent rules,
   ``n / (1 + (n - 1) * mean rho)`` (``rho`` floored at 0). Two copies of
   one rule count once, and uncorrelated rules end up equally weighted.
3. Adjust for costs: each weight is multiplied by
   ``max(0, 1 + cost_slope * (mean cost - rule cost))`` with costs in
   Sharpe units per year, then all are rescaled to sum 1. A rule 0.05 SR
   dearer than the average loses about 10% of its weight at the default
   slope of 2.
"""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd
from scipy.cluster.hierarchy import linkage, to_tree
from scipy.spatial.distance import squareform

from stonks.features.forecast_weights.base import (
    ForecastWeightEstimator,
    WeightInput,
    normalise,
    register_weight_estimator,
)


def effective_count(corr: np.ndarray) -> float:
    """``n / (1 + (n - 1) * mean off-diagonal rho)``, rho floored at 0."""
    n = corr.shape[0]
    if n < 2:
        return float(n)
    off = corr[~np.eye(n, dtype=bool)]
    mean_rho = max(0.0, float(np.mean(off)))
    return n / (1.0 + (n - 1) * mean_rho)


def _correlation(frame: pd.DataFrame) -> np.ndarray:
    corr = frame.corr().to_numpy(dtype=float)
    corr = np.nan_to_num(corr, nan=0.0)  # a flat stream: treat as uncorrelated
    np.fill_diagonal(corr, 1.0)
    return corr


@register_weight_estimator("handcraft")
class HandcraftWeights(ForecastWeightEstimator):
    """Top-down correlation groups with a cost adjustment (module doc)."""

    def __init__(self, cost_slope: float = 2.0) -> None:
        if cost_slope < 0:
            raise ValueError(f"cost_slope must be >= 0, got {cost_slope}")
        self.cost_slope = float(cost_slope)

    def estimate(self, inp: WeightInput) -> dict[str, float]:
        rules = inp.rules
        if len(rules) <= 1:
            return dict.fromkeys(rules, 1.0)
        base = self._tree_weights(_correlation(inp.net_returns), rules)
        return normalise(self._cost_adjusted(base, inp.cost_sr))

    @staticmethod
    def _tree_weights(corr: np.ndarray, rules: list[str]) -> dict[str, float]:
        dist = np.sqrt(np.clip((1.0 - corr) / 2.0, 0.0, None))
        np.fill_diagonal(dist, 0.0)
        root = to_tree(linkage(squareform(dist, checks=False), method="average"))
        weights = np.zeros(len(rules))
        stack = [(root, 1.0)]
        while stack:
            node, w = stack.pop()
            if node.is_leaf():
                weights[node.id] += w
                continue
            left, right = node.get_left(), node.get_right()
            li, ri = left.pre_order(), right.pre_order()
            nl = effective_count(corr[np.ix_(li, li)])
            nr = effective_count(corr[np.ix_(ri, ri)])
            stack.append((left, w * nl / (nl + nr)))
            stack.append((right, w * nr / (nl + nr)))
        return {r: float(weights[i]) for i, r in enumerate(rules)}

    def _cost_adjusted(
        self, base: Mapping[str, float], cost_sr: Mapping[str, float]
    ) -> dict[str, float]:
        costs = {r: float(cost_sr.get(r, 0.0)) for r in base}
        mean_cost = sum(costs.values()) / len(costs)
        return {
            r: w * max(0.0, 1.0 + self.cost_slope * (mean_cost - costs[r])) for r, w in base.items()
        }
