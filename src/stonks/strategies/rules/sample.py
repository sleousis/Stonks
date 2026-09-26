"""An in-memory, lake-shaped bar store with synthetic data, used to smoke
check strategies (and in tests) without touching the real lake."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np
import pandas as pd

from stonks.core.timeutil import as_datetime


def synthetic_bars(n: int = 300, *, seed: int = 0, start: str = "2024-01-01") -> pd.DataFrame:
    """A deterministic daily random walk with OHLCV columns."""
    rng = np.random.default_rng(seed)
    closes = 100.0 * np.exp(np.cumsum(rng.normal(0.0005, 0.015, n)))
    spread = closes * 0.01
    return pd.DataFrame(
        {
            "timestamp": pd.bdate_range(start, periods=n),
            "open": closes + rng.normal(0, 0.2, n),
            "high": closes + spread,
            "low": closes - spread,
            "close": closes,
            "adj_close": closes,
            "volume": rng.integers(100_000, 1_000_000, n).astype(float),
        }
    )


class SampleLake:
    """Serves ``get_bars`` / ``get_asset_classes`` from in-memory frames.
    Every interval returns the same frame (it is sample data)."""

    def __init__(
        self,
        frames: Mapping[str, pd.DataFrame],
        asset_classes: Mapping[str, str] | None = None,
    ) -> None:
        self._frames = {t: f.reset_index(drop=True) for t, f in frames.items()}
        self._classes = dict(asset_classes or {})

    @property
    def tickers(self) -> list[str]:
        return sorted(self._frames)

    def get_bars(self, ticker: str, interval: Any, start: Any, end: Any) -> pd.DataFrame:
        frame = self._frames.get(ticker)
        if frame is None:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
        ts = pd.to_datetime(frame["timestamp"])
        mask = (ts >= pd.Timestamp(as_datetime(start))) & (ts <= pd.Timestamp(as_datetime(end)))
        return frame.loc[mask].reset_index(drop=True)

    def get_asset_classes(self, tickers: list[str]) -> dict[str, str]:
        return {t: self._classes[t] for t in tickers if t in self._classes}
