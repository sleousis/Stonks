"""SqliteState atomicity: migrations apply all-or-nothing together with
their ``schema_migrations`` row, and ``transaction()`` nests safely."""

from __future__ import annotations

import pytest

from stonks.store import state as state_mod
from stonks.store.state import SqliteState


@pytest.fixture
def state(tmp_path):
    s = SqliteState(tmp_path / "state.sqlite")
    s.migrate()
    yield s
    s.close()


def test_failed_migration_leaves_no_partial_schema(tmp_path, monkeypatch):
    migs = tmp_path / "migs"
    migs.mkdir()
    (migs / "001_ok.sql").write_text("CREATE TABLE a (x INTEGER);")
    (migs / "002_broken.sql").write_text(
        "CREATE TABLE b (x INTEGER);\nINSERT INTO a VALUES (1);\nTHIS IS NOT SQL;\n"
    )
    monkeypatch.setattr(state_mod, "MIGRATIONS_DIR", migs)
    s = SqliteState(tmp_path / "state.sqlite")
    try:
        with pytest.raises(Exception, match="syntax error"):
            s.migrate()
        assert s.applied_migrations() == [1]
        assert "b" not in s.tables()
        assert s.count_rows("a") == 0
        assert not s.con.in_transaction

        # Fixing the script lets the next migrate() apply it cleanly.
        (migs / "002_broken.sql").write_text("CREATE TABLE b (x INTEGER);")
        s.migrate()
        assert s.applied_migrations() == [1, 2]
        assert "b" in s.tables()
    finally:
        s.close()


def test_migration_and_version_row_commit_together(tmp_path, monkeypatch):
    """If recording the version fails, the script's effects roll back."""
    migs = tmp_path / "migs"
    migs.mkdir()
    # The script itself claims version 1, so the runner's own
    # schema_migrations INSERT hits a PK violation.
    (migs / "001_self_recording.sql").write_text(
        "CREATE TABLE a (x INTEGER);\nINSERT INTO schema_migrations VALUES (1, 'x');\n"
    )
    monkeypatch.setattr(state_mod, "MIGRATIONS_DIR", migs)
    s = SqliteState(tmp_path / "state.sqlite")
    try:
        with pytest.raises(Exception, match="UNIQUE"):
            s.migrate()
        assert "a" not in s.tables()
        assert s.applied_migrations() == []
    finally:
        s.close()


def _insert_strategy(state, sid):
    state.execute(
        "INSERT INTO strategies (id, class_path, params_json, status, created_at, updated_at) "
        "VALUES (?, 'x:Y', '{}', 'shadow', 't', 't')",
        [sid],
    )


def test_nested_transaction_joins_outer(state):
    with state.transaction():
        _insert_strategy(state, "outer")
        with state.transaction():
            _insert_strategy(state, "inner")
    assert state.count_rows("strategies") == 2


def test_nested_transaction_rolls_back_with_outer(state):
    with pytest.raises(RuntimeError, match="boom"), state.transaction():
        with state.transaction():
            _insert_strategy(state, "inner")
        raise RuntimeError("boom")
    assert state.count_rows("strategies") == 0
    assert not state.con.in_transaction


def test_inner_failure_rolls_back_whole_transaction(state):
    with pytest.raises(RuntimeError, match="boom"), state.transaction():
        _insert_strategy(state, "outer")
        with state.transaction():
            _insert_strategy(state, "inner")
            raise RuntimeError("boom")
    assert state.count_rows("strategies") == 0
    assert not state.con.in_transaction
