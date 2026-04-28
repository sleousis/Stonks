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
