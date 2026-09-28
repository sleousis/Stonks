"""insider_transactions must dedup on its natural key even when parts of
that key are NULL.

DuckDB unique indexes treat NULLs as distinct, so the original
``uq_insider_natural`` index (migration 005) never fired for STOCK-Act
style rows where ``shares`` (or ``owner_name`` / ``sec_link``) is NULL,
and every re-ingest appended another copy. Migration 010 replaces that
index with a NULL-safe ``natural_key`` column.
"""

from __future__ import annotations

import shutil
from datetime import date

import pandas as pd
import pytest

from stonks.store import lake as lake_mod
from stonks.store.lake import DuckDBLake


def _row(**overrides):
    row = {
        "ticker": "AAPL.US",
        "transaction_date": date(2025, 1, 2),
        "filing_date": None,
        "owner_name": "Nancy",
        "owner_cik": None,
        "owner_relation": None,
        "owner_title": None,
        "transaction_code": "P",
        "acquired_disposed": "A",
        "shares": None,
        "price": None,
        "value": 15000.0,
        "post_transaction_amount": None,
        "sec_link": None,
    }
    row.update(overrides)
    return row


def test_duckdb_unique_index_treats_nulls_as_distinct():
    """Documents the DuckDB behaviour that motivates migration 010."""
    import duckdb

    con = duckdb.connect()
    con.execute("CREATE TABLE t (a INT, b VARCHAR, v INT)")
    con.execute("CREATE UNIQUE INDEX ut ON t (a, b)")
    for v in (1, 2):
        con.execute(
            "INSERT INTO t VALUES (1, NULL, ?) ON CONFLICT (a, b) DO UPDATE SET v = EXCLUDED.v",
            [v],
        )
    assert con.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2


@pytest.mark.parametrize(
    "overrides",
    [
        {},  # shares + sec_link NULL
        {"shares": 100.0, "sec_link": None},
        {"owner_name": None, "sec_link": "https://sec/1"},
        {"transaction_code": None, "shares": 5.0, "sec_link": "https://sec/2"},
    ],
)
def test_reingest_with_null_key_parts_does_not_duplicate(lake, overrides):
    lake.upsert_insider_transactions(pd.DataFrame([_row(**overrides)]))
    lake.upsert_insider_transactions(pd.DataFrame([_row(**overrides, value=16000.0)]))
    got = lake.sql("SELECT value FROM insider_transactions")
    assert got["value"].tolist() == [16000.0]


def test_distinct_key_parts_stay_distinct(lake):
    lake.upsert_insider_transactions(
        pd.DataFrame(
            [
                _row(),
                _row(shares=0.0),
                _row(sec_link=""),
                _row(owner_name="Paul"),
            ]
        )
    )
    assert lake.count_rows("insider_transactions") == 4


def test_int_and_float_shares_are_the_same_key(lake):
    lake.upsert_insider_transactions(pd.DataFrame([_row(shares=100.0)]))
    frame = pd.DataFrame([_row(shares=100)])
    frame["shares"] = frame["shares"].astype("int64")
    lake.upsert_insider_transactions(frame)
    assert lake.count_rows("insider_transactions") == 1


def test_migration_010_removes_existing_null_duplicates(tmp_path, monkeypatch):
    """Upgrade path: a lake that already accumulated NULL-key duplicates
    under the old index is cleaned up by migration 010 (keeping the most
    recently inserted copy), and later upserts hit the new key."""
    old_dir = tmp_path / "old_migs"
    old_dir.mkdir()
    for p in sorted(lake_mod.MIGRATIONS_DIR.glob("*.sql")):
        if int(p.stem.split("_", 1)[0]) < 10:
            shutil.copy(p, old_dir / p.name)
    real_dir = lake_mod.MIGRATIONS_DIR
    # The rebuild is data-preserving, so no destructive opt-in is needed.
    monkeypatch.delenv("STONKS_ALLOW_DESTRUCTIVE_MIGRATIONS", raising=False)
    monkeypatch.setattr(lake_mod, "MIGRATIONS_DIR", old_dir)

    lk = DuckDBLake(tmp_path / "lake.duckdb")
    try:
        lk.migrate()
        # the old schema has no known_at (DuckDB 024)
        cols = [c for c in DuckDBLake._INSIDER_COLS if c != "known_at"]
        dupes = pd.DataFrame([_row(value=1.0), _row(value=2.0), _row(value=3.0), _row(shares=7.0)])[
            cols
        ]
        lk.con.register("_seed", dupes)
        lk.con.execute(f"INSERT INTO insider_transactions ({', '.join(cols)}) SELECT * FROM _seed")
        lk.con.unregister("_seed")
        assert lk.count_rows("insider_transactions") == 4

        monkeypatch.setattr(lake_mod, "MIGRATIONS_DIR", real_dir)
        lk.migrate()
        assert 10 in lk.applied_migrations()
        got = lk.sql("SELECT shares, value FROM insider_transactions ORDER BY value")
        assert got["value"].tolist() == [3.0, 15000.0]

        # Keys computed by the migration match keys computed by the upsert.
        lk.upsert_insider_transactions(pd.DataFrame([_row(value=4.0)]))
        lk.upsert_insider_transactions(pd.DataFrame([_row(shares=7.0, value=5.0)]))
        got = lk.sql("SELECT value FROM insider_transactions ORDER BY value")
        assert got["value"].tolist() == [4.0, 5.0]
    finally:
        lk.close()
