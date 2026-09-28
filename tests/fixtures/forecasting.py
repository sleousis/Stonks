"""Fake forecasters and a lake with predictable returns (roadmap 23.11).

``ar_lake`` writes daily bars whose log returns follow an AR(1) with
coefficient ``phi``, so yesterday's return predicts today's. ``ArForecaster``
knows ``phi`` and has real skill on that lake; ``NoiseForecaster`` has none.
Neither has a ``name``, so the registry never sees them.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from pathlib import Path
from statistics import NormalDist
from typing import Any, ClassVar

import numpy as np
import pandas as pd

from stonks.core.interval import Interval
from stonks.features.forecasters.base import (
    DEFAULT_LEVELS,
    Forecast,
    Forecaster,
    context_closes,
)
from stonks.store.lake import DuckDBLake

SIGMA = 0.01


def ar_lake(
    path: Path | str,
    tickers: Sequence[str],
    *,
    periods: int = 400,
    phi: float = 0.5,
    seed: int = 11,
    start: str = "2023-01-02",
) -> DuckDBLake:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, periods=periods)
    lake = DuckDBLake(Path(path))
    lake.migrate()
    frames = []
    for ticker in tickers:
        r = np.zeros(periods)
        eps = rng.normal(0.0, SIGMA, periods)
        for t in range(1, periods):
            r[t] = phi * r[t - 1] + eps[t]
        close = 100.0 * np.exp(np.cumsum(r))
        frames.append(
            pd.DataFrame(
                {
                    "ticker": ticker,
                    "timestamp": dates,
                    "open": close,
                    "high": close * 1.002,
                    "low": close * 0.998,
                    "close": close,
                    "adj_close": close,
                    "volume": 10_000,
                }
            )
        )
    lake.upsert_bars(pd.concat(frames, ignore_index=True), Interval.DAY_1)
    return lake


def _normal(last: float, centre: float, scale: np.ndarray, levels: Sequence[float]) -> Forecast:
    z = np.array([NormalDist().inv_cdf(x) for x in levels])
    h = len(scale)
    q = last * np.exp(centre + z[:, None] * scale[None, :])
    q[:, : h - 1] = last  # only the last step matters to the tests
    return Forecast(last, tuple(levels), q)


class ArForecaster(Forecaster):
    """Knows the AR(1): ``E[r_{t+1..t+h}] = sum_k phi^k r_t``."""

    def __init__(self, phi: float = 0.5) -> None:
        self.phi = phi
        self.calls = 0

    def predict_batch(
        self,
        contexts: Sequence[pd.DataFrame],
        horizon: int,
        levels: Sequence[float] = DEFAULT_LEVELS,
    ) -> list[Forecast]:
        self.calls += len(contexts)
        out = []
        for c in contexts:
            closes = context_closes(c)
            r = float(np.log(closes[-1] / closes[-2]))
            mean = sum(self.phi**k * r for k in range(1, horizon + 1))
            scale = np.full(horizon, SIGMA * np.sqrt(horizon))
            out.append(_normal(float(closes[-1]), mean, scale, levels))
        return out


class NoiseForecaster(Forecaster):
    """Random guesses: no skill."""

    def __init__(self, seed: int = 0) -> None:
        self.rng = np.random.default_rng(seed)

    def predict_batch(
        self,
        contexts: Sequence[pd.DataFrame],
        horizon: int,
        levels: Sequence[float] = DEFAULT_LEVELS,
    ) -> list[Forecast]:
        out = []
        for c in contexts:
            closes = context_closes(c)
            mean = float(self.rng.normal(0, 2 * SIGMA))
            out.append(_normal(float(closes[-1]), mean, np.full(horizon, SIGMA), levels))
        return out


class PretrainedAr(ArForecaster):
    """The skilled model, but pretrained with weights out on 2023-12-29."""

    pretrained: ClassVar[bool] = True
    release_date: ClassVar[date | None] = date(2023, 12, 29)
    licence: ClassVar[str] = "MIT"


class ForecastUser:
    """A minimal strategy that runs ``model`` through the seam."""

    label_horizon_bars = 0
    forecast_horizon = 1
    forecast_context_bars = 32

    def __init__(self, model: Forecaster | None) -> None:
        self._model = model

    def forecaster(self) -> Any:
        return self._model
