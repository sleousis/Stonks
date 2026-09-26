"""Synthetic bar builders for the neurotrader888 chart-pattern strategy tests.

Prices are piecewise linear between hand-placed knots, so every swing point
(shoulder, head, pole tip, ...) sits exactly where the test puts it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from stonks.core.interval import Interval
from stonks.store.lake import DuckDBLake

TICKER = "X.US"
START = datetime(2026, 1, 1)


def knots_path(knots: list[tuple[int, float]]) -> np.ndarray:
    """Closes linearly interpolated between ``(bar, price)`` knots."""
    xs = [k[0] for k in knots]
    ys = [k[1] for k in knots]
    return np.interp(np.arange(xs[-1] + 1), xs, ys)


def timestamps(n: int) -> list[datetime]:
    """One daily bar per calendar day (crypto-style, no weekend gaps)."""
    return [START + timedelta(days=i) for i in range(n)]


def bars_frame(
    closes: np.ndarray,
    spread: float = 0.3,
    highs: np.ndarray | None = None,
    lows: np.ndarray | None = None,
    ticker: str = TICKER,
) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    high = closes + spread if highs is None else highs
    low = closes - spread if lows is None else lows
    return pd.DataFrame(
        {
            "ticker": ticker,
            "timestamp": timestamps(len(closes)),
            "open": closes,
            "high": high,
            "low": low,
            "close": closes,
            "adj_close": closes,
            "volume": 1_000.0,
        }
    )


def make_lake(path: Path, frame: pd.DataFrame) -> DuckDBLake:
    lake = DuckDBLake(path / "lake.duckdb")
    lake.migrate()
    lake.upsert_bars(frame, Interval.DAY_1)
    return lake


def as_of(i: int) -> datetime:
    return START + timedelta(days=i)
