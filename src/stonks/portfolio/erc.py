"""Equal risk contribution (Maillard, Roncalli and Teiletche 2010; Pfaff;
BL-44).

Each name's share of portfolio variance, ``w_i (Σw)_i / w'Σw``, equals its
risk budget ``b_i`` (equal by default). The weights come from the convex
problem ``min ½ y'Σy - Σ b_i ln y_i`` over ``y > 0`` (scipy L-BFGS-B), then
``w = y / sum(y)``. On uncorrelated names ERC is inverse volatility.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
from scipy.optimize import minimize

from stonks.portfolio._risk_based import CovarianceView, RiskBasedConstructor, RiskBasedSettings
from stonks.portfolio.base import register_constructor


def erc_weights(cov: np.ndarray, budgets: np.ndarray | None = None) -> np.ndarray:
    """Weights (summing to 1) whose risk contributions match ``budgets``
    (normalised to 1; equal when ``None``)."""
    c = np.atleast_2d(np.asarray(cov, dtype=float))
    n = c.shape[0]
    b = np.full(n, 1.0 / n) if budgets is None else np.asarray(budgets, dtype=float)
    if b.shape != (n,) or np.any(b <= 0) or not np.all(np.isfinite(b)):
        raise ValueError(f"risk budgets must be {n} positive finite numbers, got {b}")
    b = b / b.sum()
    if n == 1:
        return np.ones(1)
    # scale-free: dividing Σ by its mean variance only rescales y
    scale = float(np.mean(np.diag(c)))
    s = c / scale if scale > 0 else c
    s = 0.5 * (s + s.T)

    def objective(y: np.ndarray) -> tuple[float, np.ndarray]:
        sy = s @ y
        return 0.5 * float(y @ sy) - float(b @ np.log(y)), sy - b / y

    y0 = np.sqrt(b) / np.sqrt(np.clip(np.diag(s), 1e-12, None))
    result = minimize(
        objective,
        y0,
        jac=True,
        method="L-BFGS-B",
        bounds=[(1e-12, None)] * n,
        options={"ftol": 1e-15, "gtol": 1e-12, "maxiter": 10_000},
    )
    y = np.clip(result.x, 1e-12, None)
    return y / y.sum()


class ErcSettings(RiskBasedSettings):
    budget: Literal["equal", "score"] = "equal"


@register_constructor("erc")
class EqualRiskContribution(RiskBasedConstructor):
    """ERC over the ``top_n`` best positive combined scores. With
    ``budget="score"`` each name's risk budget is its combined score."""

    Settings = ErcSettings

    def raw_weights(self, cov: CovarianceView, scores: np.ndarray) -> np.ndarray:
        budgets = scores if self.settings.budget == "score" else None  # type: ignore[attr-defined]
        return erc_weights(cov.matrix, budgets)
