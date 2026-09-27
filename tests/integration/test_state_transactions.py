"""SqliteState atomicity: migrations apply all-or-nothing together with
their ``schema_migrations`` row, and ``transaction()`` nests safely."""

from __future__ import annotations

import pytest

from stonks.store import state as state_mod
from stonks.store.state import SqliteState


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


# ---- concurrency and interrupts (DS-07, DS-09) --------------------------------------


def test_read_then_write_waits_for_a_concurrent_writer(tmp_path):
    import threading

    path = tmp_path / "state.sqlite"
    a = SqliteState(path)
    a.migrate()
    errors: list[BaseException] = []

    def other_writer() -> None:
        # a second connection opened on the writer's own thread
        other = SqliteState(path)
        try:
            _insert_strategy(other, "b")
        except BaseException as exc:
            errors.append(exc)
        finally:
            other.close()

    try:
        with a.transaction():
            a.sql("SELECT COUNT(*) FROM strategies")
            thread = threading.Thread(target=other_writer)
            thread.start()
            thread.join(timeout=0.5)  # B commits here unless A holds the lock
            _insert_strategy(a, "a")
        thread.join(timeout=15)
        assert not thread.is_alive()
        assert errors == []
        assert a.count_rows("strategies") == 2
    finally:
        a.close()


def test_state_sets_a_busy_timeout(state):
    assert state.sql("PRAGMA busy_timeout")[0][0] >= 10_000


def test_state_transaction_rolls_back_on_keyboard_interrupt(state):
    with pytest.raises(KeyboardInterrupt), state.transaction():
        _insert_strategy(state, "x")
        raise KeyboardInterrupt
    assert not state.con.in_transaction
    assert state.count_rows("strategies") == 0
    with state.transaction():
        _insert_strategy(state, "y")
    assert state.count_rows("strategies") == 1


def test_lake_transaction_rolls_back_on_keyboard_interrupt(tmp_path):
    from stonks.store.lake import DuckDBLake

    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    try:
        with pytest.raises(KeyboardInterrupt), lake.transaction():
            lake.con.execute("INSERT INTO lake_settings VALUES ('k', 'v')")
            raise KeyboardInterrupt
        with lake.transaction():
            lake.con.execute("INSERT INTO lake_settings VALUES ('k2', 'v')")
        keys = [r[0] for r in lake.con.execute("SELECT key FROM lake_settings").fetchall()]
        assert "k" not in keys and "k2" in keys
    finally:
        lake.close()


# ---- migration encoding (DS-18) -----------------------------------------------------


def test_state_migrations_are_read_as_utf8(tmp_path, monkeypatch):
    migs = tmp_path / "migs"
    migs.mkdir()
    (migs / "001_a.sql").write_bytes(
        "CREATE TABLE t (x TEXT);\nINSERT INTO t VALUES ('café €');\n".encode()
    )
    monkeypatch.setattr(state_mod, "MIGRATIONS_DIR", migs)
    s = SqliteState(tmp_path / "state.sqlite")
    try:
        s.migrate()
        assert s.sql("SELECT x FROM t")[0][0] == "café €"
    finally:
        s.close()


def test_lake_migrations_are_read_as_utf8(tmp_path, monkeypatch):
    from stonks.store import lake as lake_mod

    migs = tmp_path / "migs"
    migs.mkdir()
    (migs / "001_a.sql").write_bytes(
        "CREATE TABLE t (x VARCHAR);\nINSERT INTO t VALUES ('café €');\n".encode()
    )
    monkeypatch.setattr(lake_mod, "MIGRATIONS_DIR", migs)
    lake = lake_mod.DuckDBLake(tmp_path / "lake.duckdb")
    try:
        lake.migrate()
        assert lake.con.execute("SELECT x FROM t").fetchone()[0] == "café €"
    finally:
        lake.close()
