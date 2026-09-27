"""Bootstrapped forecast weights (Carver; pysystemtrade's ``bootstrap``
optimiser).

Draw ``draws`` resamples of the rules' net-of-cost returns in blocks of
``block`` bars (keeping the serial correlation of trend returns), find the
long-only maximum Sharpe weights of each resample, and average them. The
average is far more stable than one optimisation over the whole sample,
and because the returns are net of costs a costly rule loses weight on
its own. Seeded, so the same data gives the same weights.
"""

from __future__ import annotations

import numpy as np
from scipy.optimize import minimize

from stonks.features.forecast_weights.base import (
    ForecastWeightEstimator,
    WeightInput,
    normalise,
    register_weight_estimator,
)


def max_sharpe_weights(returns: np.ndarray) -> np.ndarray:
    """Long-only weights summing to 1 that maximise ``w'mu / sqrt(w'Sw)``."""
    n = returns.shape[1]
    mu = returns.mean(axis=0)
    cov = np.atleast_2d(np.cov(returns, rowvar=False)) + np.eye(n) * 1e-12
    start = np.full(n, 1.0 / n)

    def neg_sharpe(w: np.ndarray) -> float:
        var = float(w @ cov @ w)
        return -float(w @ mu) / float(np.sqrt(var)) if var > 0 else 0.0

    result = minimize(
        neg_sharpe,
        start,
        method="SLSQP",
        bounds=[(0.0, 1.0)] * n,
        constraints=[{"type": "eq", "fun": lambda w: float(w.sum()) - 1.0}],
    )
    w = np.clip(result.x if result.success else start, 0.0, None)
    return w / w.sum() if w.sum() > 0 else start


@register_weight_estimator("bootstrap")
class BootstrapWeights(ForecastWeightEstimator):
    """Average long-only max-Sharpe weights over block resamples."""

    def __init__(self, draws: int = 100, block: int = 21, seed: int = 0) -> None:
        if draws < 1 or block < 1:
            raise ValueError(f"draws and block must be >= 1, got {draws}, {block}")
        self.draws, self.block, self.seed = int(draws), int(block), int(seed)

    def estimate(self, inp: WeightInput) -> dict[str, float]:
        rules = inp.rules
        if len(rules) <= 1:
            return dict.fromkeys(rules, 1.0)
        data = inp.net_returns.to_numpy(dtype=float)
        n = len(data)
        block = min(self.block, n)
        rng = np.random.default_rng(self.seed)
        n_blocks = -(-n // block)
        total = np.zeros(len(rules))
        for _ in range(self.draws):
            starts = rng.integers(0, n - block + 1, size=n_blocks)
            rows = (starts[:, None] + np.arange(block)[None, :]).ravel()[:n]
            total += max_sharpe_weights(data[rows])
        return normalise({r: float(total[i]) for i, r in enumerate(rules)})
