"""DuckDBLake read-only opening and whole-database export (the two pieces a
lab snapshot needs: build a file once, then open it in many processes)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from stonks.store.lake import DuckDBLake


def _prices(ticker: str) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ticker": ticker,
            "date": [date(2025, 1, 2), date(2025, 1, 3)],
            "open": [1.0, 2.0],
            "high": [1.0, 2.0],
            "low": [1.0, 2.0],
            "close": [1.0, 2.0],
            "adj_close": [1.0, 2.0],
            "volume": [10, 20],
        }
    )


def test_read_only_lake_reads_but_refuses_writes(tmp_path):
    path = tmp_path / "lake.duckdb"
    with DuckDBLake(path) as lake:
        lake.migrate()
        lake.upsert_prices(_prices("A.US"))

    with DuckDBLake(path, read_only=True) as ro:
        assert ro.read_only
        assert len(ro.sql("SELECT * FROM prices")) == 2
        with pytest.raises(duckdb.Error):
            ro.upsert_prices(_prices("B.US"))


def test_several_read_only_connections_share_one_file(tmp_path):
    path = tmp_path / "lake.duckdb"
    with DuckDBLake(path) as lake:
        lake.migrate()
    with DuckDBLake(path, read_only=True) as a, DuckDBLake(path, read_only=True) as b:
        assert a.sql("SELECT COUNT(*) AS n FROM bars")["n"].iloc[0] == 0
        assert b.sql("SELECT COUNT(*) AS n FROM bars")["n"].iloc[0] == 0


def test_read_only_on_a_missing_file_raises_without_creating_it(tmp_path):
    path = tmp_path / "nested" / "missing.duckdb"
    with pytest.raises(duckdb.Error):
        DuckDBLake(path, read_only=True)
    assert not path.parent.exists()


def test_read_write_is_still_the_default(tmp_path):
    with DuckDBLake(tmp_path / "lake.duckdb") as lake:
        assert not lake.read_only


def test_export_database_writes_a_complete_standalone_file(tmp_path):
    src = DuckDBLake(Path(":memory:"))
    try:
        src.migrate()
        src.upsert_prices(_prices("A.US"))
        target = tmp_path / "snap" / "copy.duckdb"
        src.export_database(target)
    finally:
        src.close()

    with DuckDBLake(target, read_only=True) as copy:
        assert list(copy.sql("SELECT close FROM prices ORDER BY date")["close"]) == [1.0, 2.0]
        # schema_migrations came along, so the copy is fully migrated
        versions = copy.sql("SELECT COUNT(*) AS n FROM schema_migrations")["n"].iloc[0]
        assert versions > 0


def test_export_database_releases_the_target_file(tmp_path):
    target = tmp_path / "copy.duckdb"
    with DuckDBLake(Path(":memory:")) as src:
        src.migrate()
        src.export_database(target)
        # the source stays usable and no longer holds the target
        target.unlink()
        assert src.sql("SELECT 1 AS x")["x"].iloc[0] == 1


def test_export_database_refuses_to_overwrite(tmp_path):
    target = tmp_path / "copy.duckdb"
    target.write_bytes(b"")
    with DuckDBLake(Path(":memory:")) as src, pytest.raises(FileExistsError):
        src.export_database(target)
