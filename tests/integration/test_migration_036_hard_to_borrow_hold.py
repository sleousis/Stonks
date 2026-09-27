"""Migration 036 (roadmap 19.16): ``order_tickets.hold`` takes
``hard_to_borrow``. The table is rebuilt to widen its check, and its rows,
indexes and the append-only trigger survive."""

from __future__ import annotations

import shutil
import sqlite3

import pytest

import stonks.store.state as state_module
from stonks.store.state import SqliteState

NOW = "2026-09-28T20:00:00+00:00"


def _ticket(state: SqliteState, ticket_id: str, hold: str | None, status: str) -> None:
    state.execute(
        "INSERT INTO order_tickets (id, portfolio_id, as_of, client_id, ticker, side, quantity,"
        " order_json, hold, status, submit_after, expires_at, created_at, updated_at)"
        " VALUES (?, 'pf_default', '2026-09-28', ?, 'GME.US', 'sell', 10, '{}', ?, ?, ?, ?, ?, ?)",
        [ticket_id, f"c-{ticket_id}", hold, status, NOW, NOW, NOW, NOW],
    )


@pytest.fixture
def state(tmp_path):
    s = SqliteState(tmp_path / "state.sqlite")
    s.migrate()
    yield s
    s.close()


def test_hold_takes_hard_to_borrow(state):
    _ticket(state, "tkt_1", "hard_to_borrow", "awaiting_approval")
    with pytest.raises(sqlite3.IntegrityError):
        _ticket(state, "tkt_2", "whatever", "awaiting_approval")


def test_rows_indexes_and_trigger_survive(tmp_path, monkeypatch):
    old = tmp_path / "old"
    old.mkdir()
    for path in sorted(state_module.MIGRATIONS_DIR.glob("*.sql")):
        if int(path.stem.split("_", 1)[0]) < 36:
            shutil.copy(path, old / path.name)
    monkeypatch.setattr(state_module, "MIGRATIONS_DIR", old)
    s = SqliteState(tmp_path / "state.sqlite")
    try:
        s.migrate()
        _ticket(s, "tkt_a", "runaway", "awaiting_approval")
        _ticket(s, "tkt_b", None, "approved")
        monkeypatch.undo()
        s.migrate()
        rows = s.sql("SELECT id, hold, status FROM order_tickets ORDER BY id")
        assert [tuple(r) for r in rows] == [
            ("tkt_a", "runaway", "awaiting_approval"),
            ("tkt_b", None, "approved"),
        ]
        indexes = {
            r["name"]
            for r in s.sql(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'order_tickets'"
            )
        }
        assert {
            "idx_order_tickets_portfolio",
            "idx_order_tickets_due",
            "idx_order_tickets_tick",
        } <= indexes
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            s.execute("DELETE FROM order_tickets WHERE id = 'tkt_a'")
        with pytest.raises(sqlite3.IntegrityError):
            _ticket(s, "tkt_c", "hard_to_borrow", "rejected")  # a rejection needs a reason
    finally:
        s.close()
