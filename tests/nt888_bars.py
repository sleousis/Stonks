"""Synthetic hourly bars + tmp-lake seeding shared by the neurotrader888
strategy tests."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from stonks.core.interval import Interval
from stonks.store.lake import DuckDBLake

START = datetime(2026, 1, 1)


def timestamps(n: int, start: datetime = START) -> list[datetime]:
    return [start + timedelta(hours=i) for i in range(n)]


def bars(
    closes,
    *,
    spread=None,
    volume=None,
    ts: list[datetime] | None = None,
) -> pd.DataFrame:
    """Hourly OHLCV frame (no ticker column). ``spread`` is the full
    high-low range per bar (scalar or array, default 1% of close)."""
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    spread = closes * 0.01 if spread is None else np.broadcast_to(np.asarray(spread, float), n)
    volume = np.full(n, 1000.0) if volume is None else np.broadcast_to(np.asarray(volume, float), n)
    ts = timestamps(n) if ts is None else ts
    opens = np.concatenate([[closes[0]], closes[:-1]])
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": opens,
            "high": np.maximum(opens, closes) + spread / 2,
            "low": np.minimum(opens, closes) - spread / 2,
            "close": closes,
            "adj_close": closes,
            "volume": volume,
        }
    )


def seed_lake(path: Path, frames: Mapping[str, pd.DataFrame]) -> DuckDBLake:
    lake = DuckDBLake(path)
    lake.migrate()
    for ticker, frame in frames.items():
        df = frame.copy()
        df.insert(0, "ticker", ticker)
        lake.upsert_bars(df, interval=Interval.HOUR_1)
    return lake


def as_of(frame: pd.DataFrame, i: int) -> datetime:
    """Timestamp of bar ``i`` (negative indices allowed)."""
    return pd.Timestamp(frame["timestamp"].iloc[i]).to_pydatetime()


def random_walk(n: int, seed: int = 0, start_price: float = 100.0) -> pd.DataFrame:
    """Hourly random walk with alternating calm / volatile regimes and
    volume that rises with the bar range (so VSA fits have a positive
    slope)."""
    rng = np.random.default_rng(seed)
    regime = np.where((np.arange(n) // 60) % 2 == 0, 0.004, 0.012)
    rets = rng.normal(0.0002, regime)
    closes = start_price * np.exp(np.cumsum(rets))
    spread = closes * regime * rng.uniform(0.5, 1.5, n)
    volume = 1000.0 * spread / (closes * 0.008) * rng.uniform(0.7, 1.3, n)
    return bars(closes, spread=spread, volume=volume)
