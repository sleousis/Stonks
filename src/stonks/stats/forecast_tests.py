"""Forecast evaluation (roadmap 23.11): Diebold-Mariano with HAC errors,
MASE, pinball loss and CRPS from quantiles, and the cross-sectional rank IC.

- :func:`diebold_mariano` (Diebold and Mariano 1995) compares two loss
  series. ``d = loss_base - loss_model``; the null is equal accuracy, the
  one-sided alternative is that the model is better (``mean(d) > 0``). The
  standard error is Newey-West with at least ``horizon - 1`` lags, because
  overlapping ``h``-step errors are autocorrelated to that order.
- :func:`mase` (Hyndman and Koehler 2006) is the mean absolute error
  scaled per forecast by the in-sample error of the naive forecast.
- :func:`crps_from_quantiles` approximates the continuous ranked
  probability score by twice the mean pinball loss over the levels (exact
  in the limit of a dense, even grid).
- :func:`rank_ic` is the Spearman correlation of forecasts with outcomes
  across names on each date, averaged over dates.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import NormalDist
from typing import Any

import numpy as np
import pandas as pd

from stonks.stats.hac import default_lags, newey_west_se

__all__ = [
    "DieboldMariano",
    "crps_from_quantiles",
    "diebold_mariano",
    "mase",
    "pinball_loss",
    "rank_ic",
]

#: Fewest names on a date for its rank IC to count.
MIN_NAMES = 3


@dataclass(frozen=True)
class DieboldMariano:
    stat: float
    p_value: float
    mean_diff: float
    n: int
    lags: int


def diebold_mariano(
    loss_base: Any, loss_model: Any, horizon: int = 1, lags: int | None = None
) -> DieboldMariano:
    """One-sided DM test that the model's losses are lower (module doc).
    Pairs with a non-finite loss are dropped."""
    a = np.asarray(loss_base, dtype=float)
    b = np.asarray(loss_model, dtype=float)
    if a.shape != b.shape:
        raise ValueError(f"loss series differ in shape: {a.shape} and {b.shape}")
    keep = np.isfinite(a) & np.isfinite(b)
    d = a[keep] - b[keep]
    n = int(d.size)
    if n < 2:
        raise ValueError(f"need at least 2 paired losses, got {n}")
    n_lags = max(int(horizon) - 1, default_lags(n)) if lags is None else int(lags)
    n_lags = min(n_lags, n - 1)
    mean = float(d.mean())
    se = newey_west_se(d, n_lags)
    if se <= 0.0 or not math.isfinite(se):
        stat = math.inf if mean > 0 else (-math.inf if mean < 0 else 0.0)
    else:
        stat = mean / se
    if stat == 0.0:
        p = 1.0
    elif math.isinf(stat):
        p = 0.0 if stat > 0 else 1.0
    else:
        p = 1.0 - NormalDist().cdf(stat)
    return DieboldMariano(float(stat), float(p), mean, n, n_lags)


def mase(errors: Any, scales: Any) -> float:
    """Mean of ``|error| / scale`` over forecasts with a positive scale."""
    e = np.abs(np.asarray(errors, dtype=float))
    s = np.asarray(scales, dtype=float)
    keep = np.isfinite(e) & np.isfinite(s) & (s > 0)
    return float(np.mean(e[keep] / s[keep])) if keep.any() else math.nan


def pinball_loss(levels: Sequence[float], quantiles: Any, y: Any) -> float:
    """Mean pinball loss. ``quantiles`` is ``n x len(levels)``, ``y`` has n."""
    tau = np.asarray(levels, dtype=float)[None, :]
    q = np.asarray(quantiles, dtype=float)
    diff = np.asarray(y, dtype=float)[:, None] - q
    loss = np.maximum(tau * diff, (tau - 1.0) * diff)
    return float(np.mean(loss))


def crps_from_quantiles(levels: Sequence[float], quantiles: Any, y: Any) -> float:
    """CRPS approximated by twice the mean pinball loss (module doc)."""
    return 2.0 * pinball_loss(levels, quantiles, y)


def rank_ic(
    dates: Any, predicted: Any, realised: Any, min_names: int = MIN_NAMES
) -> tuple[float, int]:
    """``(mean IC, dates counted)``. Dates with fewer than ``min_names``
    names, or with constant forecasts or outcomes, are skipped."""
    frame = pd.DataFrame(
        {
            "date": np.asarray(dates),
            "p": np.asarray(predicted, dtype=float),
            "r": np.asarray(realised, dtype=float),
        }
    ).dropna()
    ics = []
    for _, group in frame.groupby("date", sort=True):
        if len(group) < min_names or group["p"].nunique() < 2 or group["r"].nunique() < 2:
            continue
        ic = group["p"].rank().corr(group["r"].rank())
        if math.isfinite(ic):
            ics.append(float(ic))
    return (float(np.mean(ics)) if ics else math.nan), len(ics)
