"""Shared fixtures for integration tests in the lab / backtest area."""

from __future__ import annotations

import pandas as pd
import pytest

from stonks.core.protocols import SurvivalReport
from stonks.ingest.pipeline import _rows_to_df
from stonks.ingest.schemas import TickerProfile
from stonks.registry.store import StrategyRegistry
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from tests.fixtures.governance import seed_status


@pytest.fixture
def lake_trending(tmp_path):
    """Lake with three equity tickers spanning ~6 months:

    - UP.US   : linear uptrend 100→200
    - FLAT.US : constant 50
    - DOWN.US : linear downtrend 100→60

    Instrument profiles are seeded with ``asset_class='equity'`` so the
    Ranker's per-class filter can resolve them. Tests that want to
    exercise the "missing profile → skipped" path should use a different
    fixture or override.
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
    lake.upsert_instrument_profile(
        _rows_to_df(
            [TickerProfile(id=t, asset_class="equity") for t in ("UP.US", "FLAT.US", "DOWN.US")]
        )
    )
    yield lake
    lake.close()


@pytest.fixture
def tick_env(tmp_path, lake_trending):
    """``(lake, state, registry)`` with one active BuyAndHold on UP.US
    (BE-25). Modules that seed other strategies override it."""
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
    sid = registry.register(
        BuyAndHold({"ticker": "UP.US", "allocation": 1.0}),
        reports=[SurvivalReport(test_id="oos", passed=True, metrics={})],
    )
    seed_status(registry, sid, "active")
    yield lake_trending, state, registry
    state.close()
