"""Integration tests for DuckDBLake.aggregate_bars — time-bucket based
derivation of coarser-interval bars from stored source bars.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.store.lake import DuckDBLake


@pytest.fixture
def lake(tmp_path):
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    yield lake
    lake.close()


def _hourly_bars(n: int, start: datetime) -> pd.DataFrame:
    # synthetic rising 1h bars: open = i, close = i+1, vol = 100
    rows = []
    for i in range(n):
        ts = start + timedelta(hours=i)
        rows.append(
            {
                "ticker": "X.US",
                "timestamp": ts,
                "open": float(i),
                "high": float(i + 1.5),
                "low": float(i - 0.5),
                "close": float(i + 1),
                "adj_close": float(i + 1),
                "volume": 100,
            }
        )
    return pd.DataFrame(rows)


def test_aggregate_1h_to_4h(lake):
    start = datetime(2026, 4, 1, 0, 0, tzinfo=UTC)
    bars = _hourly_bars(24, start)
    lake.upsert_bars(bars, interval=Interval.HOUR_1)

    lake.aggregate_bars("X.US", source=Interval.HOUR_1, target=Interval.HOUR_4)

    out = lake.get_bars(
        "X.US",
        interval=Interval.HOUR_4,
        start=datetime(2026, 3, 31, 0, 0, tzinfo=UTC),
        end=datetime(2026, 4, 2, 0, 0, tzinfo=UTC),
    )
    # 24 hourly bars ⇒ 6 four-hour buckets
    assert len(out) == 6
    # First 4h bar covers hours 0..3: open=0, close=4, high=max(1.5..4.5)=4.5, low=min(-0.5..2.5)=-0.5
    first = out.sort_values("timestamp").iloc[0]
    assert first["open"] == 0.0
    assert first["close"] == 4.0
    assert first["high"] == 4.5
    assert first["low"] == -0.5
    assert first["volume"] == 400  # 4 × 100


def test_aggregate_rejects_same_or_finer_target(lake):
    with pytest.raises(ValueError, match="coarser"):
        lake.aggregate_bars("X.US", source=Interval.HOUR_1, target=Interval.HOUR_1)
    with pytest.raises(ValueError, match="coarser"):
        lake.aggregate_bars("X.US", source=Interval.HOUR_4, target=Interval.HOUR_1)


def test_aggregate_noop_on_empty_source(lake):
    # No source rows yet — aggregate should succeed silently, produce nothing.
    lake.aggregate_bars("X.US", source=Interval.HOUR_1, target=Interval.HOUR_4)
    got = lake.get_bars(
        "X.US",
        interval=Interval.HOUR_4,
        start=datetime(2000, 1, 1, tzinfo=UTC),
        end=datetime(2030, 1, 1, tzinfo=UTC),
    )
    assert got.empty


def test_aggregate_1d_to_1w(lake):
    # Seed 21 daily bars (~3 weeks)
    start = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)  # Monday
    rows = []
    for i in range(21):
        ts = start + timedelta(days=i)
        rows.append(
            {
                "ticker": "Y.US",
                "timestamp": ts,
                "open": 100.0 + i,
                "high": 100.5 + i,
                "low": 99.5 + i,
                "close": 100.2 + i,
                "adj_close": 100.2 + i,
                "volume": 1_000_000,
            }
        )
    lake.upsert_bars(pd.DataFrame(rows), interval=Interval.DAY_1)

    lake.aggregate_bars("Y.US", source=Interval.DAY_1, target=Interval.WEEK_1)

    weekly = lake.get_bars(
        "Y.US",
        interval=Interval.WEEK_1,
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 2, 1, tzinfo=UTC),
    )
    # 21 days ⇒ 3 weeks (each time_bucket on 1 week contains 7 rows exactly here)
    assert len(weekly) == 3
    first_week = weekly.sort_values("timestamp").iloc[0]
    assert first_week["volume"] == 7 * 1_000_000


def test_aggregate_is_idempotent(lake):
    start = datetime(2026, 4, 1, 0, 0, tzinfo=UTC)
    lake.upsert_bars(_hourly_bars(24, start), interval=Interval.HOUR_1)

    lake.aggregate_bars("X.US", source=Interval.HOUR_1, target=Interval.HOUR_4)
    first_count = lake.count_rows("bars")
    lake.aggregate_bars("X.US", source=Interval.HOUR_1, target=Interval.HOUR_4)
    second_count = lake.count_rows("bars")
    assert first_count == second_count
