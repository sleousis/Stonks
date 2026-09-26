"""ParquetBarStore: hive-partitioned bar files, atomic idempotent upserts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import duckdb
import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.store.bars import BAR_COLUMNS, ParquetBarStore


@pytest.fixture
def con():
    c = duckdb.connect()
    c.execute("SET TimeZone = 'UTC'")
    yield c
    c.close()


@pytest.fixture
def store(tmp_path, con):
    s = ParquetBarStore(tmp_path / "bars", con)
    s.ensure_layout()
    return s


def _bars(ticker, start, n, interval="1d", step=timedelta(days=1), close0=100.0):
    return pd.DataFrame(
        {
            "ticker": ticker,
            "timestamp": [start + i * step for i in range(n)],
            "interval": interval,
            "open": [close0 + i for i in range(n)],
            "high": [close0 + i + 1 for i in range(n)],
            "low": [close0 + i - 1 for i in range(n)],
            "close": [close0 + i for i in range(n)],
            "adj_close": [close0 + i for i in range(n)],
            "volume": [1000 + i for i in range(n)],
        }
    )


def _all(store):
    return store.get("A.US", Interval.DAY_1, datetime(1900, 1, 1), datetime(2200, 1, 1))


def test_upsert_writes_one_file_per_interval_ticker_year(store, tmp_path):
    store.upsert(_bars("A.US", datetime(2023, 12, 30), 4))
    files = sorted(p.relative_to(tmp_path / "bars").as_posix() for p in store.files())
    assert files == [
        "interval=1d/ticker=A.US/year=2023/part-0.parquet",
        "interval=1d/ticker=A.US/year=2024/part-0.parquet",
    ]
    assert len(_all(store)) == 4


def test_no_temp_files_remain_after_a_write(store, tmp_path):
    store.upsert(_bars("A.US", datetime(2024, 1, 1), 3))
    leftovers = [p for p in (tmp_path / "bars").rglob("*") if p.suffix == ".tmp"]
    assert leftovers == []


def test_upsert_is_idempotent_and_last_write_wins(store):
    store.upsert(_bars("A.US", datetime(2024, 1, 1), 5))
    store.upsert(_bars("A.US", datetime(2024, 1, 1), 5))
    assert len(_all(store)) == 5
    fix = _bars("A.US", datetime(2024, 1, 3), 1, close0=999.0)
    store.upsert(fix)
    got = _all(store)
    assert len(got) == 5
    assert got.loc[got["timestamp"] == datetime(2024, 1, 3), "close"].item() == 999.0


def test_duplicate_keys_inside_one_batch_keep_the_last_row(store):
    batch = pd.concat(
        [_bars("A.US", datetime(2024, 1, 2), 1, close0=1.0)]
        + [_bars("A.US", datetime(2024, 1, 2), 1, close0=2.0)],
        ignore_index=True,
    )
    assert store.upsert(batch) == 2
    assert _all(store)["close"].tolist() == [2.0]


def test_get_returns_the_table_shape_ordered_by_timestamp(store):
    frame = _bars("A.US", datetime(2024, 1, 1), 5).iloc[::-1]
    store.upsert(frame)
    got = store.get("A.US", Interval.DAY_1, datetime(2024, 1, 2), datetime(2024, 1, 4))
    assert list(got.columns) == [c for c in BAR_COLUMNS if c != "interval"]
    assert got["timestamp"].tolist() == [datetime(2024, 1, d) for d in (2, 3, 4)]
    assert set(got["ticker"]) == {"A.US"}


def test_get_on_a_missing_series_is_empty_with_table_dtypes(store, con):
    got = store.get("NONE.US", Interval.DAY_1, datetime(2024, 1, 1), datetime(2024, 2, 1))
    assert got.empty
    assert list(got.columns) == [c for c in BAR_COLUMNS if c != "interval"]
    assert str(got["timestamp"].dtype).startswith("datetime64")
    assert got["volume"].dtype == "int64"


def test_tz_aware_input_is_stored_as_naive_utc(store):
    start = datetime(2024, 3, 1, 14, 30, tzinfo=UTC)
    store.upsert(_bars("A.US", start, 2, interval="5m", step=timedelta(minutes=5)))
    got = store.get(
        "A.US", Interval.MIN_5, datetime(2024, 3, 1, tzinfo=UTC), datetime(2024, 3, 2, tzinfo=UTC)
    )
    assert got["timestamp"].tolist() == [datetime(2024, 3, 1, 14, 30), datetime(2024, 3, 1, 14, 35)]


def test_tickers_with_path_hostile_characters_round_trip(store, con):
    for ticker in ("ES=F", "^GSPC", "BRK/B.US"):
        store.upsert(_bars(ticker, datetime(2024, 1, 1), 2))
        got = store.get(ticker, Interval.DAY_1, datetime(2024, 1, 1), datetime(2024, 2, 1))
        assert len(got) == 2 and set(got["ticker"]) == {ticker}
    view = con.execute(f"SELECT DISTINCT ticker FROM ({store.view_sql()})").fetchall()
    assert sorted(t for (t,) in view) == ["BRK/B.US", "ES=F", "^GSPC"]


def test_numeric_looking_tickers_stay_strings(store, con):
    store.upsert(_bars("1234", datetime(2024, 1, 1), 1))
    got = con.execute(f"SELECT ticker FROM ({store.view_sql()})").fetchall()
    assert got == [("1234",)]


def test_view_over_an_empty_store_has_the_bars_schema(store, con):
    cols = con.execute(f"SELECT * FROM ({store.view_sql()})").description
    assert [c[0] for c in cols] == list(BAR_COLUMNS)
    assert con.execute(f"SELECT COUNT(*) FROM ({store.view_sql()})").fetchone()[0] == 0


def test_view_sees_every_series(store, con):
    store.upsert(_bars("A.US", datetime(2024, 1, 1), 3))
    store.upsert(_bars("B.US", datetime(2024, 1, 1), 2, interval="1h", step=timedelta(hours=1)))
    rows = con.execute(
        f"SELECT ticker, interval, COUNT(*) FROM ({store.view_sql()}) GROUP BY ALL ORDER BY 1"
    ).fetchall()
    assert rows == [("A.US", "1d", 3), ("B.US", "1h", 2)]


def test_a_partition_with_stray_files_is_compacted_on_the_next_write(store, con):
    store.upsert(_bars("A.US", datetime(2024, 1, 1), 2))
    part = store.partition_dir("1d", "A.US", 2024)
    stray = part / "part-extra.parquet"
    con.execute(
        f"COPY (SELECT TIMESTAMP '2024-01-10' AS timestamp, 1.0::DOUBLE AS open, 1.0::DOUBLE AS high,"
        f" 1.0::DOUBLE AS low, 1.0::DOUBLE AS close, 1.0::DOUBLE AS adj_close, 5::BIGINT AS volume)"
        f" TO '{stray.as_posix()}' (FORMAT parquet)"
    )
    (part / "part-0.parquet.123.tmp").write_bytes(b"half-written")
    store.upsert(_bars("A.US", datetime(2024, 1, 5), 1))
    assert sorted(p.name for p in part.iterdir()) == ["part-0.parquet"]
    assert len(_all(store)) == 4


def test_case_colliding_tickers_are_refused(store):
    store.upsert(_bars("abc.US", datetime(2024, 1, 1), 1))
    with pytest.raises(ValueError, match="case"):
        store.upsert(_bars("ABC.US", datetime(2024, 1, 1), 1))


def test_read_only_store_refuses_writes(tmp_path, con):
    store = ParquetBarStore(tmp_path / "bars", con, read_only=True)
    with pytest.raises(duckdb.Error):
        store.upsert(_bars("A.US", datetime(2024, 1, 1), 1))


def test_aggregate_builds_coarser_bars(store):
    store.upsert(
        _bars("A.US", datetime(2024, 1, 2, 0, 0), 8, interval="1h", step=timedelta(hours=1))
    )
    assert store.aggregate("A.US", Interval.HOUR_1, Interval.HOUR_4) == 2
    assert store.aggregate("A.US", Interval.HOUR_1, Interval.HOUR_4) == 0
    got = store.get("A.US", Interval.HOUR_4, datetime(2024, 1, 1), datetime(2024, 1, 3))
    assert got["open"].tolist() == [100.0, 104.0]
    assert got["close"].tolist() == [103.0, 107.0]
    assert got["volume"].tolist() == [1000 + 1001 + 1002 + 1003, 1004 + 1005 + 1006 + 1007]


def test_checksums_cover_every_series(store):
    store.upsert(_bars("A.US", datetime(2024, 1, 1), 3))
    store.upsert(_bars("B.US", datetime(2024, 1, 1), 2))
    sums = store.series_checksums()
    assert set(sums) == {("A.US", "1d"), ("B.US", "1d")}
    assert sums[("A.US", "1d")][0] == 3


def test_export_partitions_copies_a_universe_up_to_an_end_date(store, tmp_path, con):
    store.upsert(_bars("A.US", datetime(2023, 12, 28), 10))
    store.upsert(_bars("B.US", datetime(2023, 12, 28), 10))
    target = ParquetBarStore(tmp_path / "snap" / "bars", con, read_only=True)
    store.export_partitions(target.root, tickers=["A.US"], end=datetime(2024, 1, 2).date())
    got = target.get("A.US", Interval.DAY_1, datetime(1900, 1, 1), datetime(2200, 1, 1))
    assert got["timestamp"].max() == datetime(2024, 1, 2)
    assert len(got) == 6
    assert target.get("B.US", Interval.DAY_1, datetime(1900, 1, 1), datetime(2200, 1, 1)).empty
    # the source is untouched and later writes do not leak into the copy
    store.upsert(_bars("A.US", datetime(2023, 12, 28), 1, close0=-1.0))
    again = target.get("A.US", Interval.DAY_1, datetime(1900, 1, 1), datetime(2200, 1, 1))
    assert again["close"].iloc[0] == 100.0
