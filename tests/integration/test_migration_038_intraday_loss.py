"""Migration 038 (roadmap 21.3.2): the ``intraday_loss`` halt kind.

``risk_halts`` is rebuilt to widen its kind CHECK, keeping its rows, ids,
AUTOINCREMENT counter, indexes and guards. ``reconcile_reports`` refers to
it, so it is rebuilt around it with its rows."""

from __future__ import annotations

import sqlite3

import pytest

from stonks.production.halts import HALT_KINDS, clear_halt, trip_halt
from stonks.store import state as state_module
from stonks.store.state import SqliteState


@pytest.fixture
def state(tmp_path):
    s = SqliteState(tmp_path / "state.sqlite")
    s.migrate()
    yield s
    s.close()


def _migrate_to(state: SqliteState, last: int) -> None:
    state.con.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    for file in sorted(state_module.MIGRATIONS_DIR.glob("*.sql")):
        version = int(file.stem.split("_", 1)[0])
        if version > last:
            break
        state.con.executescript(
            f"BEGIN;\n{file.read_text(encoding='utf-8')}\n;\n"
            f"INSERT INTO schema_migrations VALUES ({version}, 'x');\nCOMMIT;"
        )


def test_intraday_loss_is_a_halt_kind(state):
    assert "intraday_loss" in HALT_KINDS
    halt, created = trip_halt(
        state, "intraday_loss", reason="-2% in 5 min", actor="system", portfolio_id="pf_default"
    )
    assert created and halt.kind == "intraday_loss"
    clear_halt(state, halt.id, actor="user:usr_owner", reason="checked the book")


def test_the_guards_survive(state):
    halt, _ = trip_halt(
        state, "intraday_loss", reason="loss", actor="system", portfolio_id="pf_default"
    )
    with pytest.raises(sqlite3.IntegrityError):
        state.execute("DELETE FROM risk_halts")
    with pytest.raises(sqlite3.IntegrityError):
        state.execute("UPDATE risk_halts SET reason = 'x'")
    with pytest.raises(sqlite3.IntegrityError):  # one open halt per kind and target
        state.execute(
            "INSERT INTO risk_halts (kind, scope, portfolio_id, reason, tripped_by, tripped_at)"
            " VALUES ('intraday_loss', 'portfolio', 'pf_default', 'again', 'system', 'x')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        state.execute(
            "INSERT INTO risk_halts (kind, scope, portfolio_id, reason, tripped_by, tripped_at)"
            " VALUES ('made_up', 'portfolio', 'pf_default', 'r', 'system', 'x')"
        )
    assert halt.id > 0


def test_rows_ids_counter_and_reports_survive_the_rebuild(tmp_path):
    old = SqliteState(tmp_path / "old.sqlite")
    _migrate_to(old, 37)
    kill, _ = trip_halt(old, "kill", reason="stop", actor="user:usr_owner", scope="global", halt="all")
    drift, _ = trip_halt(
        old, "broker_drift", reason="one share", actor="system", portfolio_id="pf_default"
    )
    clear_halt(old, drift.id, actor="user:usr_owner", reason="explained")
    old.execute(
        "INSERT INTO reconcile_reports (id, portfolio_id, kind, as_of, taken_at, status, halt_id)"
        " VALUES ('rec_1', 'pf_default', 'eod', '2026-09-25', 'x', 'drift', ?)",
        [drift.id],
    )
    old.migrate()
    rows = old.sql("SELECT id, kind, halt, reason, cleared_at FROM risk_halts ORDER BY id")
    assert [(r["id"], r["kind"], r["halt"]) for r in rows] == [
        (kill.id, "kill", "all"),
        (drift.id, "broker_drift", "buys"),
    ]
    assert rows[1]["cleared_at"] is not None
    report = old.sql("SELECT halt_id, status FROM reconcile_reports")[0]
    assert (report["halt_id"], report["status"]) == (drift.id, "drift")
    fks = old.sql("PRAGMA foreign_key_list(reconcile_reports)")
    assert [r["table"] for r in fks] == ["risk_halts"]
    new, _ = trip_halt(
        old, "intraday_loss", reason="loss", actor="system", portfolio_id="pf_default"
    )
    assert new.id == drift.id + 1
    assert old.sql("PRAGMA foreign_key_check") == []
    old.close()


def test_reconcile_reports_keeps_its_checks(state):
    with pytest.raises(sqlite3.IntegrityError):
        state.execute(
            "INSERT INTO reconcile_reports (id, portfolio_id, kind, as_of, taken_at, status)"
            " VALUES ('rec_x', 'pf_default', 'weekly', '2026-09-25', 'x', 'clean')"
        )
    with pytest.raises(sqlite3.IntegrityError):  # the halt must exist
        state.execute(
            "INSERT INTO reconcile_reports (id, portfolio_id, kind, as_of, taken_at, status,"
            " halt_id) VALUES ('rec_y', 'pf_default', 'eod', '2026-09-25', 'x', 'drift', 999)"
        )
    names = {r["name"] for r in state.sql("PRAGMA index_list(reconcile_reports)")}
    assert "idx_reconcile_reports_portfolio" in names
