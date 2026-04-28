"""Integration tests for the interval-aware ``bars`` table (migration 003)
and its Python wrappers.

The daily ``get_prices`` / ``upsert_prices`` methods stay as back-compat
shims over the same underlying ``bars`` table, so no downstream code needs
to change for daily data.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

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


def _bar(ticker, ts, close):
    return {
        "ticker": ticker,
        "timestamp": ts,
        "open": close - 0.1,
        "high": close + 0.1,
        "low": close - 0.2,
        "close": close,
        "adj_close": close,
        "volume": 1_000_000,
    }


def _intraday_bars(ticker="AAPL.US"):
    start = datetime(2026, 4, 1, 9, 30, tzinfo=UTC)
    return pd.DataFrame(
        [
            _bar(ticker, start.replace(minute=start.minute + 5 * i), 100.0 + i) for i in range(6)
        ]  # 6 bars: 09:30, 09:35, 09:40, 09:45, 09:50, 09:55
    )


# ---- migration + schema ----------------------------------------------------


def test_migration_003_creates_bars_table_and_prices_view(lake):
    assert "bars" in lake.tables()
    # `prices` remains available (as a view) for backward-compat SQL
    assert (
        "prices"
        in lake.sql("SELECT table_name FROM information_schema.tables WHERE table_name='prices'")
        .to_dict()["table_name"]
        .values()
    )


# ---- upsert_bars + get_bars ------------------------------------------------


def test_upsert_bars_roundtrip(lake):
    df = _intraday_bars()
    assert lake.upsert_bars(df, interval=Interval.MIN_5) == 6

    got = lake.get_bars(
        "AAPL.US",
        interval=Interval.MIN_5,
        start=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
        end=datetime(2026, 4, 1, 23, 59, tzinfo=UTC),
    )
    assert len(got) == 6


def test_upsert_bars_is_idempotent(lake):
    df = _intraday_bars()
    lake.upsert_bars(df, interval=Interval.MIN_5)
    lake.upsert_bars(df, interval=Interval.MIN_5)
    assert lake.count_rows("bars") == 6


def test_upsert_bars_updates_on_conflict(lake):
    df = _intraday_bars()
    lake.upsert_bars(df, interval=Interval.MIN_5)

    # rewrite the first bar with a different close
    new_close = 999.0
    df2 = df.iloc[:1].copy()
    df2["close"] = new_close
    df2["adj_close"] = new_close
    lake.upsert_bars(df2, interval=Interval.MIN_5)

    got = lake.get_bars(
        "AAPL.US",
        interval=Interval.MIN_5,
        start=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
        end=datetime(2026, 4, 1, 23, 59, tzinfo=UTC),
    )
    first_row = got.sort_values("timestamp").iloc[0]
    assert first_row["close"] == new_close


def test_get_bars_returns_only_requested_interval(lake):
    # same (ticker, timestamp) with two different intervals should coexist
    min5 = _intraday_bars()
    hour1 = pd.DataFrame([_bar("AAPL.US", datetime(2026, 4, 1, 10, 0, tzinfo=UTC), 100.0)])
    lake.upsert_bars(min5, interval=Interval.MIN_5)
    lake.upsert_bars(hour1, interval=Interval.HOUR_1)

    assert lake.count_rows("bars") == 7

    got_5m = lake.get_bars(
        "AAPL.US",
        interval=Interval.MIN_5,
        start=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
        end=datetime(2026, 4, 1, 23, 59, tzinfo=UTC),
    )
    assert len(got_5m) == 6

    got_1h = lake.get_bars(
        "AAPL.US",
        interval=Interval.HOUR_1,
        start=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
        end=datetime(2026, 4, 1, 23, 59, tzinfo=UTC),
    )
    assert len(got_1h) == 1


def test_get_bars_filters_by_window(lake):
    df = _intraday_bars()
    lake.upsert_bars(df, interval=Interval.MIN_5)
    got = lake.get_bars(
        "AAPL.US",
        interval=Interval.MIN_5,
        start=datetime(2026, 4, 1, 9, 40, tzinfo=UTC),
        end=datetime(2026, 4, 1, 9, 50, tzinfo=UTC),
    )
    assert len(got) == 3  # 09:40, 09:45, 09:50


# ---- backwards-compat: daily shims -----------------------------------------


def test_upsert_prices_backwards_compat_writes_into_bars(lake):
    df = pd.DataFrame(
        [
            {
                "ticker": "AAPL.US",
                "date": date(2026, 4, 1),
                "open": 100,
                "high": 105,
                "low": 99,
                "close": 104,
                "adj_close": 104,
                "volume": 1_000_000,
            },
        ]
    )
    lake.upsert_prices(df)

    bars_rows = lake.sql("SELECT ticker, interval FROM bars WHERE ticker='AAPL.US'")
    assert len(bars_rows) == 1
    assert bars_rows.iloc[0]["interval"] == "1d"

    read = lake.get_prices("AAPL.US", date(2026, 3, 1), date(2026, 5, 1))
    assert len(read) == 1
    assert read.iloc[0]["close"] == 104


def test_prices_view_sees_only_daily_bars(lake):
    df_daily = pd.DataFrame(
        [
            {
                "ticker": "AAPL.US",
                "date": date(2026, 4, 1),
                "open": 100,
                "high": 105,
                "low": 99,
                "close": 104,
                "adj_close": 104,
                "volume": 1_000_000,
            },
        ]
    )
    lake.upsert_prices(df_daily)
    lake.upsert_bars(_intraday_bars(), interval=Interval.MIN_5)

    prices = lake.sql("SELECT * FROM prices WHERE ticker='AAPL.US'")
    assert len(prices) == 1  # only the daily one, not the 6 intraday bars


def test_upsert_bars_empty_df_is_noop(lake):
    assert lake.upsert_bars(pd.DataFrame(), interval=Interval.MIN_5) == 0
    assert lake.count_rows("bars") == 0
