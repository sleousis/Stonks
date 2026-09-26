"""Synthetic lakes for the BL-40 trend strategy tests."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from stonks.store.lake import DuckDBLake
from tests.unit.nt888_helpers import make_lake, write_bars

DATES = pd.bdate_range("2021-01-04", periods=700)
LAST = DATES[-1].to_pydatetime()


def trend(drift: float, seed: int = 0, n: int = len(DATES), vol: float = 0.01) -> np.ndarray:
    """Geometric random walk with a daily log drift."""
    rng = np.random.default_rng(seed)
    return 50.0 * np.exp(np.cumsum(drift + rng.normal(0.0, vol, n)))


def build_lake(
    path: Path,
    series: dict[str, np.ndarray],
    dates: pd.DatetimeIndex = DATES,
    asset_classes: dict[str, str] | None = None,
) -> DuckDBLake:
    """A lake with daily bars for each ticker; a series shorter than
    ``dates`` covers the *last* dates (a late listing)."""
    lake = make_lake(path)
    for ticker, closes in series.items():
        write_bars(lake, ticker, dates[len(dates) - len(closes) :], closes)
    for ticker, cls in (asset_classes or {}).items():
        lake.con.execute("INSERT INTO instruments (id, asset_class) VALUES (?, ?)", [ticker, cls])
    return lake
