"""DuckDBLake over either bar store (roadmap 10.4): the Parquet store is a
drop-in for the ``bars`` table, the lake remembers which one it uses, and
``migrate_bars_to_parquet`` / ``migrate_bars_to_duckdb`` move between them
with a checksum check."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.store import bars_migrate
from stonks.store.lake import DuckDBLake

BACKENDS = ["duckdb", "parquet"]


def _daily(ticker: str, start: date, n: int, *, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 + rng.normal(0, 1, n).cumsum()
    return pd.DataFrame(
        {
            "ticker": ticker,
            "date": [start + timedelta(days=i) for i in range(n)],
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "adj_close": close,
            "volume": rng.integers(1, 10_000, n),
        }
    )


def _intraday(ticker: str, n: int) -> pd.DataFrame:
    start = datetime(2024, 12, 31, 20, 0)
    return pd.DataFrame(
        {
            "ticker": ticker,
            "timestamp": [start + timedelta(minutes=5 * i) for i in range(n)],
            "open": np.arange(n, dtype=float),
            "high": np.arange(n, dtype=float) + 1,
            "low": np.arange(n, dtype=float) - 1,
            "close": np.arange(n, dtype=float) + 0.5,
            "adj_close": np.arange(n, dtype=float) + 0.5,
            "volume": np.arange(n),
        }
    )


def _fill(lake: DuckDBLake) -> None:
    lake.upsert_prices(_daily("A.US", date(2023, 12, 1), 90, seed=1))
    lake.upsert_prices(_daily("B.US", date(2023, 12, 1), 90, seed=2))
    lake.upsert_bars(_intraday("A.US", 60), Interval.MIN_5)
    lake.upsert_bars(_intraday("ES=F", 10), Interval.MIN_5)


def _everything(lake: DuckDBLake) -> pd.DataFrame:
    return lake.sql("SELECT * FROM bars ORDER BY ticker, interval, timestamp")


@pytest.fixture(params=BACKENDS)
def lake(tmp_path, request):
    lk = DuckDBLake(tmp_path / "lake.duckdb", bar_backend=request.param)
    lk.migrate()
    yield lk
    lk.close()


# ---- the same lake API on both stores ----------------------------------------


def test_bar_backend_is_reported(lake, request):
    assert lake.bar_backend == request.node.callspec.params["lake"]


def test_bars_relation_and_prices_view_work_on_both(lake):
    _fill(lake)
    assert "bars" in lake.tables()
    assert lake.count_rows("bars") == 90 * 2 + 60 + 10
    assert len(lake.sql("SELECT * FROM prices WHERE ticker = 'A.US'")) == 90
    got = lake.get_prices("B.US", date(2024, 1, 1), date(2024, 1, 31))
    assert len(got) == 31


def test_duplicate_keys_in_a_batch_keep_the_last_row(lake):
    frame = _daily("X.US", date(2024, 1, 2), 1)
    lake.upsert_prices(pd.concat([frame.assign(close=2.0), frame.assign(close=3.0)]))
    assert lake.sql("SELECT close FROM bars")["close"].tolist() == [3.0]


def test_tz_aware_input_lands_as_naive_utc(lake):
    frame = _intraday("A.US", 2)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"]).dt.tz_localize("America/New_York")
    lake.upsert_bars(frame, Interval.MIN_5)
    got = lake.sql("SELECT timestamp FROM bars ORDER BY timestamp")["timestamp"].tolist()
    assert got == [pd.Timestamp("2025-01-01 01:00"), pd.Timestamp("2025-01-01 01:05")]


def test_both_stores_return_identical_frames(tmp_path):
    frames = {}
    for backend in BACKENDS:
        with DuckDBLake(tmp_path / backend / "lake.duckdb", bar_backend=backend) as lk:
            lk.migrate()
            _fill(lk)
            lk.aggregate_bars("A.US", Interval.MIN_5, Interval.HOUR_1)
            frames[backend] = (
                _everything(lk),
                lk.get_bars("A.US", Interval.MIN_5, datetime(2024, 1, 1), datetime(2026, 1, 1)),
                lk.get_prices("A.US", date(2023, 1, 1), date(2026, 1, 1)),
            )
    for table_frame, parquet_frame in zip(*frames.values(), strict=True):
        pd.testing.assert_frame_equal(table_frame, parquet_frame)


# ---- the Parquet store specifically ------------------------------------------


def test_parquet_lake_writes_hive_partitions_next_to_the_lake(tmp_path):
    with DuckDBLake(tmp_path / "lake.duckdb", bar_backend="parquet") as lk:
        lk.migrate()
        _fill(lk)
        assert lk.bars_root == tmp_path / "bars"
    files = sorted(
        p.relative_to(tmp_path / "bars").as_posix()
        for p in (tmp_path / "bars").glob("interval=*/*/*/*.parquet")
        if "interval=_" not in p.as_posix()
    )
    assert "interval=1d/ticker=A.US/year=2023/part-0.parquet" in files
    assert "interval=5m/ticker=ES%3DF/year=2024/part-0.parquet" in files
    # no bars table in the DuckDB file any more
    con = duckdb.connect(str(tmp_path / "lake.duckdb"), read_only=True)
    try:
        assert (
            con.execute(
                "SELECT COUNT(*) FROM duckdb_tables() WHERE table_name = 'bars'"
            ).fetchone()[0]
            == 0
        )
    finally:
        con.close()


def test_the_lake_remembers_its_store_across_reopens(tmp_path):
    path = tmp_path / "lake.duckdb"
    with DuckDBLake(path, bar_backend="parquet") as lk:
        lk.migrate()
        _fill(lk)
        expected = _everything(lk)
    # callers that pass no backend (app, CLI, MCP) get the store the lake uses
    with DuckDBLake(path) as lk:
        assert lk.bar_backend == "parquet"
        pd.testing.assert_frame_equal(_everything(lk), expected)
    with DuckDBLake(path, read_only=True) as ro:
        assert ro.bar_backend == "parquet"
        pd.testing.assert_frame_equal(_everything(ro), expected)
        with pytest.raises(duckdb.Error):
            ro.upsert_prices(_daily("C.US", date(2024, 1, 1), 1))


def test_parquet_readers_run_while_a_read_write_lake_is_open(tmp_path):
    path = tmp_path / "lake.duckdb"
    writer = DuckDBLake(path, bar_backend="parquet")
    writer.migrate()
    try:
        _fill(writer)
        # a plain Parquet reader needs no DuckDB file lock at all
        con = duckdb.connect()
        n = con.execute(
            f"SELECT COUNT(*) FROM read_parquet('{(tmp_path / 'bars').as_posix()}/interval=1d/*/*/*.parquet')"
        ).fetchone()[0]
        assert n == 180
        writer.upsert_prices(_daily("C.US", date(2024, 1, 1), 5))
        n = con.execute(
            f"SELECT COUNT(*) FROM read_parquet('{(tmp_path / 'bars').as_posix()}/interval=1d/*/*/*.parquet')"
        ).fetchone()[0]
        assert n == 185
    finally:
        writer.close()


def test_asking_for_parquet_on_a_lake_with_table_bars_points_at_the_migration(tmp_path):
    path = tmp_path / "lake.duckdb"
    with DuckDBLake(path) as lk:
        lk.migrate()
        _fill(lk)
    with pytest.raises(RuntimeError, match="bars_migrate"):
        DuckDBLake(path, bar_backend="parquet")


def test_asking_for_the_table_on_a_parquet_lake_is_refused(tmp_path):
    path = tmp_path / "lake.duckdb"
    with DuckDBLake(path, bar_backend="parquet") as lk:
        lk.migrate()
    with pytest.raises(RuntimeError, match="migrate_bars_to_duckdb"):
        DuckDBLake(path, bar_backend="duckdb")


def test_in_memory_lakes_cannot_use_parquet():
    with pytest.raises(ValueError, match="memory"):
        DuckDBLake(Path(":memory:"), bar_backend="parquet").migrate()


# ---- migration tool --------------------------------------------------------------


def test_migrate_bars_to_parquet_copies_verifies_and_switches(tmp_path):
    path = tmp_path / "lake.duckdb"
    with DuckDBLake(path) as lk:
        lk.migrate()
        _fill(lk)
        before = _everything(lk)
        report = lk.migrate_bars_to_parquet()
        assert lk.bar_backend == "parquet"
        assert report.series == 4 and report.rows == len(before)
        pd.testing.assert_frame_equal(_everything(lk), before)
        # writes go to Parquet from now on
        lk.upsert_prices(_daily("C.US", date(2024, 1, 1), 3))
        assert (tmp_path / "bars" / "interval=1d" / "ticker=C.US").is_dir()
    with DuckDBLake(path) as lk:
        assert lk.bar_backend == "parquet"
        assert lk.count_rows("bars") == len(before) + 3


def test_migrate_bars_to_parquet_leaves_the_table_when_verification_fails(tmp_path, monkeypatch):
    path = tmp_path / "lake.duckdb"
    with DuckDBLake(path) as lk:
        lk.migrate()
        _fill(lk)
        from stonks.store import bars as bars_module

        real = bars_module.ParquetBarStore.series_checksums

        def corrupt(self):
            out = real(self)
            key = next(iter(out))
            out[key] = (out[key][0] - 1, out[key][1])
            return out

        monkeypatch.setattr(bars_module.ParquetBarStore, "series_checksums", corrupt)
        with pytest.raises(RuntimeError, match="checksum"):
            lk.migrate_bars_to_parquet()
        assert lk.bar_backend == "duckdb"
        assert lk.count_rows("bars") == 90 * 2 + 70


def test_migrate_back_to_the_table(tmp_path):
    path = tmp_path / "lake.duckdb"
    with DuckDBLake(path, bar_backend="parquet") as lk:
        lk.migrate()
        _fill(lk)
        before = _everything(lk)
        report = lk.migrate_bars_to_duckdb()
        assert report.rows == len(before)
        assert lk.bar_backend == "duckdb"
        pd.testing.assert_frame_equal(_everything(lk), before)
    with DuckDBLake(path) as lk:
        assert lk.bar_backend == "duckdb"
        pd.testing.assert_frame_equal(_everything(lk), before)


def test_migration_entry_point_follows_the_config(tmp_path, capsys):
    path = tmp_path / "lake.duckdb"
    with DuckDBLake(path) as lk:
        lk.migrate()
        _fill(lk)
    config = tmp_path / "stonks.toml"
    config.write_text(
        f'[lake]\npath = "{path.as_posix()}"\n\n[lake.bars]\nbackend = "parquet"\n',
        encoding="utf-8",
    )
    assert bars_migrate.main(["--config", str(config)]) == 0
    assert "parquet" in capsys.readouterr().out
    with DuckDBLake(path) as lk:
        assert lk.bar_backend == "parquet"
    # already there: a no-op
    assert bars_migrate.main(["--config", str(config)]) == 0
    assert bars_migrate.main(["--lake", str(path), "--to", "duckdb"]) == 0
    with DuckDBLake(path) as lk:
        assert lk.bar_backend == "duckdb"


# ---- export ---------------------------------------------------------------------


def test_export_database_of_a_parquet_lake_carries_its_bars(tmp_path):
    with DuckDBLake(tmp_path / "src" / "lake.duckdb", bar_backend="parquet") as lk:
        lk.migrate()
        _fill(lk)
        expected = _everything(lk)
        lk.export_database(tmp_path / "copy" / "lake.duckdb")
    with DuckDBLake(tmp_path / "copy" / "lake.duckdb", read_only=True) as copy:
        assert copy.bar_backend == "parquet"
        pd.testing.assert_frame_equal(_everything(copy), expected)


def test_export_database_can_switch_the_copy_to_parquet(tmp_path):
    with DuckDBLake(Path(":memory:")) as lk:
        lk.migrate()
        _fill(lk)
        expected = _everything(lk)
        lk.export_database(tmp_path / "copy" / "lake.duckdb", bar_backend="parquet")
    with DuckDBLake(tmp_path / "copy" / "lake.duckdb", read_only=True) as copy:
        assert copy.bar_backend == "parquet"
        pd.testing.assert_frame_equal(_everything(copy), expected)
