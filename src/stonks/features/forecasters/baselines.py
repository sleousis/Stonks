"""Cheap statistical baselines (roadmap 23.11).

Every model must beat these before it counts (the ``forecast_skill``
survival test). They work on log closes ``y`` and learn only from the
context, so they have no pretraining cutoff and add no trials (their
settings are fixed, never tuned).

- ``random_walk``: the next closes equal the last one. Bands widen with
  the context's one-bar log return sigma times ``sqrt(k)``.
- ``drift``: the random walk plus the mean log return per bar.
- ``ets``: simple exponential smoothing, ETS(A,N,N), with ``alpha`` picked
  on a grid by one-step squared error. Bands:
  ``sigma * sqrt(1 + (k - 1) * alpha^2)``.
- ``theta``: the Theta method (Assimakopoulos and Nikolopoulos), as SES
  with drift (Hyndman and Billah): the SES level plus half the least
  squares slope of ``y``, ``b/2 * (k - 1 + 1/alpha - (1 - alpha)^n / alpha)``.
  Bands as for ``ets``.

Bands are normal in log space, so the close quantiles are log-normal.
"""

from __future__ import annotations

from collections.abc import Sequence
from statistics import NormalDist
from typing import ClassVar

import numpy as np
import pandas as pd

from stonks.features.forecasters.base import (
    DEFAULT_LEVELS,
    Forecast,
    Forecaster,
    _check_levels,
    context_closes,
)

__all__ = ["DriftForecaster", "EtsForecaster", "RandomWalkForecaster", "ThetaForecaster"]

#: SES smoothing weights tried by ``ets`` and ``theta``.
_ALPHAS = np.round(np.arange(0.05, 1.0, 0.05), 2)


def _z(levels: tuple[float, ...]) -> np.ndarray:
    return np.array([NormalDist().inv_cdf(x) for x in levels])


def _log_normal(
    last: float, centre: np.ndarray, scale: np.ndarray, levels: tuple[float, ...]
) -> Forecast:
    """Quantiles ``exp(centre_k + z * scale_k)`` of the closes."""
    q = np.exp(centre[None, :] + _z(levels)[:, None] * scale[None, :])
    return Forecast(last, levels, q)


def _ses(y: np.ndarray, alpha: float) -> tuple[float, float]:
    """``(final level, one-step squared error sum)`` of SES seeded at y[0]."""
    level = float(y[0])
    sse = 0.0
    for v in y[1:]:
        err = float(v) - level
        sse += err * err
        level += alpha * err
    return level, sse


def _best_ses(y: np.ndarray) -> tuple[float, float, float]:
    """``(alpha, level, one-step sigma)`` with the lowest squared error."""
    best = min(((a, *_ses(y, float(a))) for a in _ALPHAS), key=lambda t: t[2])
    alpha, level, sse = float(best[0]), float(best[1]), float(best[2])
    sigma = float(np.sqrt(sse / max(1, len(y) - 2)))
    return alpha, level, sigma


class _LogBaseline(Forecaster):
    """Shared plumbing: checks, log closes, one context at a time."""

    licence: ClassVar[str] = ""

    def predict_batch(
        self,
        contexts: Sequence[pd.DataFrame],
        horizon: int,
        levels: Sequence[float] = DEFAULT_LEVELS,
    ) -> list[Forecast]:
        h = self._check_horizon(horizon)
        lv = _check_levels(levels)
        out = []
        for context in contexts:
            closes = context_closes(context, self.min_context)
            out.append(self._one(np.log(closes), h, lv))
        return out

    def _one(self, y: np.ndarray, h: int, levels: tuple[float, ...]) -> Forecast:
        raise NotImplementedError


class RandomWalkForecaster(_LogBaseline):
    name: ClassVar[str] = "random_walk"
    description: ClassVar[str] = "The last close carried forward (the bar to beat)."

    def _one(self, y: np.ndarray, h: int, levels: tuple[float, ...]) -> Forecast:
        steps = np.arange(1, h + 1)
        sigma = float(np.std(np.diff(y), ddof=1))
        return _log_normal(float(np.exp(y[-1])), np.full(h, y[-1]), sigma * np.sqrt(steps), levels)


class DriftForecaster(_LogBaseline):
    name: ClassVar[str] = "drift"
    description: ClassVar[str] = "The random walk plus the mean return per bar."

    def _one(self, y: np.ndarray, h: int, levels: tuple[float, ...]) -> Forecast:
        steps = np.arange(1, h + 1)
        diffs = np.diff(y)
        sigma = float(np.std(diffs, ddof=1))
        centre = y[-1] + float(np.mean(diffs)) * steps
        return _log_normal(float(np.exp(y[-1])), centre, sigma * np.sqrt(steps), levels)


def _ses_scale(sigma: float, alpha: float, h: int) -> np.ndarray:
    steps = np.arange(1, h + 1)
    return sigma * np.sqrt(1.0 + (steps - 1) * alpha**2)


class EtsForecaster(_LogBaseline):
    name: ClassVar[str] = "ets"
    description: ClassVar[str] = "Simple exponential smoothing, ETS(A,N,N), on log closes."

    def _one(self, y: np.ndarray, h: int, levels: tuple[float, ...]) -> Forecast:
        alpha, level, sigma = _best_ses(y)
        return _log_normal(
            float(np.exp(y[-1])), np.full(h, level), _ses_scale(sigma, alpha, h), levels
        )


class ThetaForecaster(_LogBaseline):
    name: ClassVar[str] = "theta"
    description: ClassVar[str] = "The Theta method: SES with half the linear trend as drift."

    def _one(self, y: np.ndarray, h: int, levels: tuple[float, ...]) -> Forecast:
        alpha, level, sigma = _best_ses(y)
        n = len(y)
        t = np.arange(n, dtype=float)
        slope = float(np.polyfit(t, y, 1)[0])
        steps = np.arange(1, h + 1)
        drift = slope / 2.0 * (steps - 1 + 1.0 / alpha - (1.0 - alpha) ** n / alpha)
        return _log_normal(float(np.exp(y[-1])), level + drift, _ses_scale(sigma, alpha, h), levels)
