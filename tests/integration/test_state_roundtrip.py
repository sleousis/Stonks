"""Integration tests for SqliteState — the foundational transactional-state
store. SqliteState is intentionally thin: connection management, migrations,
introspection, and a generic query surface. Domain helpers belong to the
blocks that own each table (registry, execution, production).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from stonks.store.state import SqliteState


@pytest.fixture
def state(tmp_path):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    yield state
    state.close()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---- migrations -------------------------------------------------------------


def test_migrate_creates_expected_tables(state):
    tables = set(state.tables())
    expected = {
        "schema_migrations",
        "strategies",
        "survival_reports",
        "tick_runs",
        "orders",
        "fills",
        "portfolio_snapshots",
    }
    assert expected <= tables


def test_migrate_is_idempotent(tmp_path):
    path = tmp_path / "state.sqlite"
    SqliteState(path).migrate()
    with SqliteState(path) as s:
        s.migrate()  # must not raise on second run
        versions = s.applied_migrations()
    assert versions == [1]


def test_applied_migrations_starts_empty_before_migrate(tmp_path):
    with SqliteState(tmp_path / "x.sqlite") as s:
        assert s.applied_migrations() == []


# ---- foreign keys + CHECK constraints ---------------------------------------


def test_foreign_keys_are_enforced(state):
    # survival_reports.strategy_id references strategies.id
    with pytest.raises(sqlite3.IntegrityError):
        state.execute(
            "INSERT INTO survival_reports "
            "(strategy_id, test_id, passed, metrics_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ["nonexistent", "oos", 1, "{}", _now()],
        )


def test_strategy_status_check_rejects_bogus_values(state):
    with pytest.raises(sqlite3.IntegrityError):
        state.execute(
            "INSERT INTO strategies "
            "(id, class_path, params_json, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ["s1", "stonks.strategies.x:X", "{}", "unknown", _now(), _now()],
        )


def test_strategy_status_accepts_valid_values(state):
    for status in ("active", "shadow", "retired"):
        state.execute(
            "INSERT INTO strategies "
            "(id, class_path, params_json, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [f"s_{status}", "x.y:Z", "{}", status, _now(), _now()],
        )
    assert state.count_rows("strategies") == 3


def test_order_side_check(state):
    state.execute(
        "INSERT INTO tick_runs (id, started_at, status) VALUES (?, ?, 'running')",
        ["t1", _now()],
    )
    with pytest.raises(sqlite3.IntegrityError):
        state.execute(
            "INSERT INTO orders "
            "(client_id, tick_id, ticker, side, quantity, order_type, status, "
            " created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ["o1", "t1", "AAPL.US", "sideways", 1.0, "market", "pending", _now(), _now()],
        )


# ---- generic query surface --------------------------------------------------


def test_strategies_pk_collision(state):
    args = ["s_dup", "x.y:Z", "{}", "shadow", _now(), _now()]
    state.execute(
        "INSERT INTO strategies (id, class_path, params_json, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        args,
    )
    with pytest.raises(sqlite3.IntegrityError):
        state.execute(
            "INSERT INTO strategies (id, class_path, params_json, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            args,
        )


def test_sql_returns_rows_as_mappings(state):
    state.execute(
        "INSERT INTO strategies (id, class_path, params_json, status, created_at, updated_at) "
        "VALUES ('s1', 'x.y:Z', '{}', 'shadow', ?, ?)",
        [_now(), _now()],
    )
    rows = state.sql("SELECT id, status FROM strategies WHERE id = ?", ["s1"])
    assert len(rows) == 1
    assert rows[0]["id"] == "s1"
    assert rows[0]["status"] == "shadow"


def test_count_rows(state):
    assert state.count_rows("strategies") == 0


# ---- transactions -----------------------------------------------------------


def test_transaction_commits_on_success(state):
    with state.transaction():
        state.execute(
            "INSERT INTO strategies (id, class_path, params_json, status, created_at, updated_at) "
            "VALUES ('s_ok', 'x.y:Z', '{}', 'shadow', ?, ?)",
            [_now(), _now()],
        )
    assert state.count_rows("strategies") == 1


def test_transaction_rolls_back_on_exception(state):
    with pytest.raises(RuntimeError), state.transaction():
        state.execute(
            "INSERT INTO strategies (id, class_path, params_json, status, created_at, updated_at) "
            "VALUES ('s_rb', 'x.y:Z', '{}', 'shadow', ?, ?)",
            [_now(), _now()],
        )
        raise RuntimeError("boom")
    assert state.count_rows("strategies") == 0


# ---- lifecycle --------------------------------------------------------------


def test_close_is_idempotent(tmp_path):
    s = SqliteState(tmp_path / "y.sqlite")
    s.close()
    s.close()  # must not raise
