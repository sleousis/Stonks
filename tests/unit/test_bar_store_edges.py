"""Bar store edge cases on both backends (review 18.1, DS-04, DS-14)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import duckdb
import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.store.bars import ParquetBarStore, open_bar_reader
from stonks.store.lake import DuckDBLake

EVER = (datetime(1900, 1, 1), datetime(2200, 1, 1))


@pytest.fixture(params=["duckdb", "parquet"])
def lake(request, tmp_path):
    lk = DuckDBLake(tmp_path / "lake.duckdb", bar_backend=request.param)
    lk.migrate()
    yield lk
    lk.close()


def _daily(ticker, start, n, close=100.0, adj=None):
    days = [start + timedelta(days=i) for i in range(n)]
    return pd.DataFrame(
        {
            "ticker": ticker,
            "date": days,
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "adj_close": close if adj is None else adj,
            "volume": 10,
        }
    )


def test_weekly_bars_keep_the_adjusted_close(lake):
    # a Monday..Friday week: raw close 100, the vendor's adjusted close 50
    lake.upsert_prices(_daily("A.US", date(2024, 6, 3), 5, close=100.0, adj=50.0))
    lake.aggregate_bars("A.US", Interval.DAY_1, Interval.WEEK_1)
    week = lake.get_bars("A.US", Interval.WEEK_1, *EVER)
    assert week["close"].tolist() == [100.0]
    assert week["adj_close"].tolist() == [50.0]


def test_delete_bars_removes_only_the_given_timestamps(lake):
    lake.upsert_prices(_daily("A.US", date(2024, 12, 30), 4))
    gone = lake.delete_bars("A.US", Interval.DAY_1, [datetime(2024, 12, 31), datetime(2025, 1, 2)])
    assert gone == 2
    left = lake.get_prices("A.US", date(2024, 1, 1), date(2026, 1, 1))["date"].tolist()
    assert left == [date(2024, 12, 30), date(2025, 1, 1)]
    assert lake.delete_bars("A.US", Interval.DAY_1, [datetime(2024, 12, 31)]) == 0


def test_tz_aware_upsert_across_the_new_year_lands_in_utc(lake):
    new_york = timezone(timedelta(hours=-5))
    stamps = [
        datetime(2024, 12, 31, 18, 55, tzinfo=new_york),  # 23:55 UTC
        datetime(2024, 12, 31, 19, 0, tzinfo=new_york),  # 00:00 UTC next year
    ]
    frame = pd.DataFrame(
        {
            "ticker": "A.US",
            "timestamp": stamps,
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
            "adj_close": 1.0,
            "volume": 1,
        }
    )
    lake.upsert_bars(frame, interval=Interval.MIN_5)
    got = lake.get_bars("A.US", Interval.MIN_5, *EVER)["timestamp"].tolist()
    assert got == [datetime(2024, 12, 31, 23, 55), datetime(2025, 1, 1, 0, 0)]


@pytest.mark.parametrize("ticker", ["A%B.US", "ABC.", "Z%2E"])
def test_odd_tickers_round_trip(lake, ticker):
    lake.upsert_prices(_daily(ticker, date(2024, 1, 1), 2))
    got = lake.get_prices(ticker, date(2024, 1, 1), date(2024, 1, 2))
    assert len(got) == 2 and set(got["ticker"]) == {ticker}
    assert ticker in set(lake.sql("SELECT DISTINCT ticker FROM bars")["ticker"])


def test_export_partitions_with_an_end_on_new_years_eve(tmp_path):
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    try:
        store = ParquetBarStore(tmp_path / "bars", con)
        store.ensure_layout()
        frame = _daily("A.US", date(2024, 12, 29), 6).rename(columns={"date": "timestamp"})
        frame["timestamp"] = pd.to_datetime(frame["timestamp"])
        frame["interval"] = "1d"
        store.upsert(frame)
        target = ParquetBarStore(tmp_path / "snap", con, read_only=True)
        store.export_partitions(target.root, end=date(2024, 12, 31))
        got = target.get("A.US", Interval.DAY_1, *EVER)
        assert got["timestamp"].max() == datetime(2024, 12, 31)
        assert len(got) == 3
        assert not list((target.root / "interval=1d" / "ticker=A.US").glob("year=2025"))
    finally:
        con.close()


def test_a_reader_opened_on_an_empty_store_sees_later_writes(tmp_path):
    root = tmp_path / "bars"
    reader = open_bar_reader(root)
    try:
        assert reader.execute("SELECT COUNT(*) FROM bars").fetchone()[0] == 0
        con = duckdb.connect()
        try:
            writer = ParquetBarStore(root, con)
            frame = _daily("A.US", date(2024, 1, 1), 3).rename(columns={"date": "timestamp"})
            frame["timestamp"] = pd.to_datetime(frame["timestamp"])
            frame["interval"] = "1d"
            writer.upsert(frame)
        finally:
            con.close()
        assert reader.execute("SELECT COUNT(*) FROM bars").fetchone()[0] == 3
        assert reader.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == 3
    finally:
        reader.close()


def test_bar_coverage_keeps_bars_in_the_last_half_second_of_the_day(lake):
    frame = pd.DataFrame(
        {
            "ticker": "A.US",
            "timestamp": [datetime(2024, 6, 3, 12), datetime(2024, 6, 3, 23, 59, 59, 500000)],
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
            "adj_close": 1.0,
            "volume": 1,
        }
    )
    lake.upsert_bars(frame, interval=Interval.MIN_1)
    cov = lake.bar_coverage(["A.US"], Interval.MIN_1, date(2024, 6, 3), date(2024, 6, 3))
    assert int(cov["n_window"].iloc[0]) == 2
    assert pd.Timestamp(cov["last_bar"].iloc[0]) == pd.Timestamp(2024, 6, 3, 23, 59, 59, 500000)


def _utc(*args):
    return datetime(*args, tzinfo=UTC)
