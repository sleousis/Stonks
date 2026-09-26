"""Clenow trend features (*Stocks on the Move*, BL-39).

- :func:`regression_momentum`: fit ``ln(close) = a + b * i`` by OLS over the
  last ``lookback`` closes; the score is the annualised slope
  ``exp(b)^annualization - 1`` times the fit's R², so a steady trend beats
  an equally steep but ragged one;
- :func:`max_gap`: the largest absolute close-to-close move in the window,
  for the "no gap over 15%" filter.

Inputs are closes up to the decision bar (oldest first); nothing reads past
the last element.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

__all__ = ["RegressionMomentum", "max_gap", "regression_momentum"]


@dataclass(frozen=True)
class RegressionMomentum:
    annualized_slope: float
    r_squared: float

    @property
    def score(self) -> float:
        return self.annualized_slope * self.r_squared


def regression_momentum(
    closes: np.ndarray, lookback: int = 90, annualization: int = 250
) -> RegressionMomentum | None:
    """Exponential-regression momentum of the last ``lookback`` closes, or
    ``None`` with too few closes or a non-positive one. A flat window has
    slope 0 and R² 0."""
    if lookback < 2:
        raise ValueError(f"lookback must be >= 2, got {lookback}")
    closes = np.asarray(closes, dtype=float)
    if len(closes) < lookback:
        return None
    window = closes[-lookback:]
    if not np.all(window > 0) or not np.all(np.isfinite(window)):
        return None
    y = np.log(window)
    x = np.arange(lookback, dtype=float)
    x_c, y_c = x - x.mean(), y - y.mean()
    sxx, syy, sxy = float(x_c @ x_c), float(y_c @ y_c), float(x_c @ y_c)
    if syy <= 1e-24:  # flat window
        return RegressionMomentum(0.0, 0.0)
    slope = sxy / sxx
    r_squared = min(1.0, sxy * sxy / (sxx * syy))
    return RegressionMomentum(math.exp(slope) ** annualization - 1.0, r_squared)


def max_gap(closes: np.ndarray, lookback: int = 90) -> float | None:
    """Largest ``|C[i] / C[i-1] - 1|`` over the last ``lookback`` daily
    returns, or ``None`` with fewer than ``lookback + 1`` closes."""
    if lookback < 1:
        raise ValueError(f"lookback must be >= 1, got {lookback}")
    closes = np.asarray(closes, dtype=float)
    if len(closes) < lookback + 1:
        return None
    window = closes[-lookback - 1 :]
    return float(np.max(np.abs(window[1:] / window[:-1] - 1.0)))
