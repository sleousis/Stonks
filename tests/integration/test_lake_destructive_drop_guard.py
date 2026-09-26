"""Integration tests for the lake's destructive-DROP migration guard.

A migration that contains a ``DROP TABLE`` against a table that holds
rows refuses to apply unless the operator opts in via
``STONKS_ALLOW_DESTRUCTIVE_MIGRATIONS=1`` — losing committed data must
be a deliberate choice, not an unannounced side-effect of
``stonks db init``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stonks.store.lake import DuckDBLake


@pytest.fixture
def empty_lake(tmp_path):
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    yield lake
    lake.close()


def _write_migration(dirpath: Path, version: int, sql: str) -> Path:
    p = dirpath / f"{version:03d}_test_drop.sql"
    p.write_text(sql)
    return p


def test_drop_against_empty_table_is_allowed(empty_lake, tmp_path, monkeypatch):
    """An ``IF EXISTS`` drop against a non-existent table must not
    require the opt-in env var — that would gate every fresh-checkout
    migration sweep on a flag that exists for safety, not ergonomics."""
    monkeypatch.setattr(
        "stonks.store.lake.MIGRATIONS_DIR",
        tmp_path / "migs",
        raising=False,
    )
    (tmp_path / "migs").mkdir()
    _write_migration(
        tmp_path / "migs",
        version=1,
        sql="DROP TABLE IF EXISTS does_not_exist; CREATE TABLE marker (x INT);",
    )
    empty_lake.migrate()  # must not raise
    assert "marker" in empty_lake.tables()


def test_drop_against_non_empty_table_refuses_without_opt_in(empty_lake, tmp_path, monkeypatch):
    """Migrate creates a `legacy` table with one row, then a second
    migration tries to drop it. Without the env var the migrate call
    raises and the row survives."""
    monkeypatch.setattr(
        "stonks.store.lake.MIGRATIONS_DIR",
        tmp_path / "migs",
        raising=False,
    )
    (tmp_path / "migs").mkdir()
    _write_migration(
        tmp_path / "migs",
        version=1,
        sql="CREATE TABLE legacy (x INT); INSERT INTO legacy VALUES (1);",
    )
    empty_lake.migrate()
    assert empty_lake.count_rows("legacy") == 1

    _write_migration(tmp_path / "migs", version=2, sql="DROP TABLE legacy;")
    monkeypatch.delenv("STONKS_ALLOW_DESTRUCTIVE_MIGRATIONS", raising=False)
    with pytest.raises(RuntimeError, match="STONKS_ALLOW_DESTRUCTIVE_MIGRATIONS"):
        empty_lake.migrate()
    # Data survives the refusal — no half-applied migration.
    assert "legacy" in empty_lake.tables()
    assert empty_lake.count_rows("legacy") == 1


def test_drop_against_non_empty_table_proceeds_with_opt_in(empty_lake, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "stonks.store.lake.MIGRATIONS_DIR",
        tmp_path / "migs",
        raising=False,
    )
    (tmp_path / "migs").mkdir()
    _write_migration(
        tmp_path / "migs",
        version=1,
        sql="CREATE TABLE legacy (x INT); INSERT INTO legacy VALUES (1);",
    )
    empty_lake.migrate()
    _write_migration(tmp_path / "migs", version=2, sql="DROP TABLE legacy;")
    monkeypatch.setenv("STONKS_ALLOW_DESTRUCTIVE_MIGRATIONS", "1")
    empty_lake.migrate()
    assert "legacy" not in empty_lake.tables()


def _stage_real_migrations(dirpath: Path, below: int) -> None:
    """Copy the shipped migrations with version < ``below`` into ``dirpath``
    so a test can build a lake frozen at an older schema version."""
    import shutil

    from stonks.store import lake as lake_mod

    dirpath.mkdir()
    for p in sorted(lake_mod.MIGRATIONS_DIR.glob("*.sql")):
        if int(p.stem.split("_", 1)[0]) < below:
            shutil.copy(p, dirpath / p.name)


def test_upgrade_through_003_preserves_prices_without_opt_in(empty_lake, tmp_path, monkeypatch):
    """003 copies every ``prices`` row into ``bars`` before dropping
    ``prices``; the guard must recognise that and not demand the opt-in."""
    from stonks.store import lake as lake_mod

    real_dir = lake_mod.MIGRATIONS_DIR
    _stage_real_migrations(tmp_path / "old", below=3)
    monkeypatch.setattr(lake_mod, "MIGRATIONS_DIR", tmp_path / "old")
    empty_lake.migrate()
    empty_lake.con.execute(
        "INSERT INTO prices VALUES ('AAPL.US', DATE '2024-01-02', 1, 2, 0.5, 1.5, 1.5, 100)"
    )

    monkeypatch.setattr(lake_mod, "MIGRATIONS_DIR", real_dir)
    monkeypatch.delenv("STONKS_ALLOW_DESTRUCTIVE_MIGRATIONS", raising=False)
    empty_lake.migrate()
    assert empty_lake.count_rows("bars") == 1
    assert empty_lake.sql("SELECT close FROM prices")["close"].tolist() == [1.5]


def test_drop_table_cascade_is_guarded(empty_lake, tmp_path, monkeypatch):
    monkeypatch.setattr("stonks.store.lake.MIGRATIONS_DIR", tmp_path / "migs")
    (tmp_path / "migs").mkdir()
    _write_migration(
        tmp_path / "migs",
        version=1,
        sql="CREATE TABLE legacy (x INT); INSERT INTO legacy VALUES (1);",
    )
    empty_lake.migrate()
    _write_migration(tmp_path / "migs", version=2, sql="DROP TABLE legacy CASCADE;")
    monkeypatch.delenv("STONKS_ALLOW_DESTRUCTIVE_MIGRATIONS", raising=False)
    with pytest.raises(RuntimeError, match="STONKS_ALLOW_DESTRUCTIVE_MIGRATIONS"):
        empty_lake.migrate()
    assert empty_lake.count_rows("legacy") == 1


def test_drop_column_with_values_is_guarded(empty_lake, tmp_path, monkeypatch):
    monkeypatch.setattr("stonks.store.lake.MIGRATIONS_DIR", tmp_path / "migs")
    (tmp_path / "migs").mkdir()
    _write_migration(
        tmp_path / "migs",
        version=1,
        sql="CREATE TABLE legacy (x INT, y INT); INSERT INTO legacy VALUES (1, 2);",
    )
    empty_lake.migrate()
    _write_migration(tmp_path / "migs", version=2, sql="ALTER TABLE legacy DROP COLUMN y;")
    monkeypatch.delenv("STONKS_ALLOW_DESTRUCTIVE_MIGRATIONS", raising=False)
    with pytest.raises(RuntimeError, match="DROP COLUMN legacy.y"):
        empty_lake.migrate()
    assert empty_lake.sql("SELECT y FROM legacy")["y"].tolist() == [2]

    monkeypatch.setenv("STONKS_ALLOW_DESTRUCTIVE_MIGRATIONS", "1")
    empty_lake.migrate()
    assert "y" not in empty_lake.sql("SELECT * FROM legacy").columns


def test_drop_column_holding_only_nulls_is_allowed(empty_lake, tmp_path, monkeypatch):
    monkeypatch.setattr("stonks.store.lake.MIGRATIONS_DIR", tmp_path / "migs")
    (tmp_path / "migs").mkdir()
    _write_migration(
        tmp_path / "migs",
        version=1,
        sql="CREATE TABLE legacy (x INT, y INT); INSERT INTO legacy VALUES (1, NULL);",
    )
    empty_lake.migrate()
    _write_migration(
        tmp_path / "migs", version=2, sql="ALTER TABLE legacy DROP COLUMN IF EXISTS y;"
    )
    monkeypatch.delenv("STONKS_ALLOW_DESTRUCTIVE_MIGRATIONS", raising=False)
    empty_lake.migrate()
    assert "y" not in empty_lake.sql("SELECT * FROM legacy").columns


def test_drop_mentioned_in_sql_comment_is_ignored(empty_lake, tmp_path, monkeypatch):
    monkeypatch.setattr("stonks.store.lake.MIGRATIONS_DIR", tmp_path / "migs")
    (tmp_path / "migs").mkdir()
    _write_migration(
        tmp_path / "migs",
        version=1,
        sql="CREATE TABLE legacy (x INT); INSERT INTO legacy VALUES (1);",
    )
    empty_lake.migrate()
    _write_migration(
        tmp_path / "migs",
        version=2,
        sql="-- a later cleanup may DROP TABLE legacy;\nCREATE TABLE marker (x INT);",
    )
    monkeypatch.delenv("STONKS_ALLOW_DESTRUCTIVE_MIGRATIONS", raising=False)
    empty_lake.migrate()
    assert "marker" in empty_lake.tables()


def test_fresh_init_needs_no_opt_in(empty_lake, monkeypatch):
    """Shipped migrations that drop tables/columns only touch empty
    tables on a fresh lake, so ``db init`` must not demand the opt-in."""
    monkeypatch.delenv("STONKS_ALLOW_DESTRUCTIVE_MIGRATIONS", raising=False)
    empty_lake.migrate()
    assert "bars" in empty_lake.tables()
