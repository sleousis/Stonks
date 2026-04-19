"""Shared fixtures for integration tests in the lab / backtest area."""

from __future__ import annotations

import pandas as pd
import pytest

from stonks.store.lake import DuckDBLake


@pytest.fixture
def lake_trending(tmp_path):
    """Lake with three tickers spanning ~6 months:

    - UP.US   : linear uptrend 100→200
    - FLAT.US : constant 50
    - DOWN.US : linear downtrend 100→60
    """
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()

    dates = pd.bdate_range(start="2025-10-01", end="2026-04-01")
    n = len(dates)

    rows = []
    for ticker, closes in [
        ("UP.US", [100.0 + 100.0 * i / (n - 1) for i in range(n)]),
        ("FLAT.US", [50.0] * n),
        ("DOWN.US", [100.0 - 40.0 * i / (n - 1) for i in range(n)]),
    ]:
        for d, c in zip(dates, closes, strict=False):
            rows.append(
                {
                    "ticker": ticker,
                    "date": d.date(),
                    "open": c,
                    "high": c + 0.5,
                    "low": c - 0.5,
                    "close": c,
                    "adj_close": c,
                    "volume": 1_000_000,
                }
            )
    lake.upsert_prices(pd.DataFrame(rows))
    yield lake
    lake.close()
