"""Migration 029 (roadmap 19.8): the ``approve`` subscription mode and the
``order_tickets`` table. The subscriptions table is rebuilt to widen its
mode check, and its rows, indexes, triggers and the position attributions
that point at it survive."""

from __future__ import annotations

import shutil
import sqlite3

import pytest

import stonks.store.state as state_module
from stonks.store.state import SqliteState

NOW = "2026-03-17T20:00:00+00:00"


@pytest.fixture
def state(tmp_path):
    s = SqliteState(tmp_path / "state.sqlite")
    s.migrate()
    yield s
    s.close()


def _user_portfolio_strategy(state: SqliteState) -> None:
    state.execute(
        "INSERT INTO strategies (id, class_path, params_json, artifact_path, status,"
        " created_at, updated_at) VALUES ('s1', 'x.Y', '{}', 'a', 'active', ?, ?)",
        [NOW, NOW],
    )


def _subscribe(state: SqliteState, sub_id: str, mode: str) -> None:
    state.execute(
        "INSERT INTO subscriptions (id, user_id, strategy_id, portfolio_id, mode, created_at,"
        " updated_at) VALUES (?, 'usr_owner', 's1', 'pf_default', ?, ?, ?)",
        [sub_id, mode, NOW, NOW],
    )


def test_subscriptions_take_the_approve_mode(state):
    _user_portfolio_strategy(state)
    _subscribe(state, "sub_a", "approve")
    with pytest.raises(sqlite3.IntegrityError):
        _subscribe(state, "sub_b", "manual")
    with pytest.raises(sqlite3.IntegrityError):
        _subscribe(state, "sub_a2", "paper")  # one subscription per (portfolio, strategy)


def test_the_owner_trigger_survives_the_rebuild(state):
    _user_portfolio_strategy(state)
    state.execute(
        "INSERT INTO users (id, kind, email, display_name, role, status, timezone, created_at)"
        " VALUES ('usr_x', 'human', 'x@example.com', 'X', 'trader', 'active', 'UTC', ?)",
        [NOW],
    )
    with pytest.raises(sqlite3.IntegrityError, match="must own the portfolio"):
        state.execute(
            "INSERT INTO subscriptions (id, user_id, strategy_id, portfolio_id, mode,"
            " created_at, updated_at) VALUES ('sub_x', 'usr_x', 's1', 'pf_default', 'paper', ?, ?)",
            [NOW, NOW],
        )


def test_rows_and_attributions_survive(tmp_path, monkeypatch):
    old = tmp_path / "old_migrations"
    old.mkdir()
    for path in sorted(state_module.MIGRATIONS_DIR.glob("*.sql")):
        if int(path.stem.split("_", 1)[0]) < 29:
            shutil.copy(path, old / path.name)
    monkeypatch.setattr(state_module, "MIGRATIONS_DIR", old)
    s = SqliteState(tmp_path / "state.sqlite")
    try:
        s.migrate()
        _user_portfolio_strategy(s)
        _subscribe(s, "sub_1", "auto")
        s.execute(
            "UPDATE subscriptions SET paper_since = ?, paused_reason = 'x' WHERE id = 'sub_1'",
            [NOW],
        )
        s.execute("INSERT INTO tick_runs (id, started_at, status) VALUES ('t1', ?, 'ok')", [NOW])
        s.execute(
            "INSERT INTO position_attribution (tick_id, portfolio_id, as_of, ticker, strategy_id,"
            " subscription_id, quantity, weight_share, source, created_at)"
            " VALUES ('t1', 'pf_default', '2026-03-17', 'A.US', 's1', 'sub_1', 1, 1, 'decision', ?)",
            [NOW],
        )
        monkeypatch.undo()
        s.migrate()
        [row] = s.sql("SELECT mode, paper_since, paused_reason FROM subscriptions")
        assert tuple(row) == ("auto", NOW, "x")
        [attr] = s.sql("SELECT subscription_id FROM position_attribution")
        assert attr[0] == "sub_1"
        assert 29 in s.applied_migrations()
        assert s.sql("PRAGMA foreign_key_check") == []
    finally:
        s.close()


def _ticket(state: SqliteState, ticket_id: str, client_id: str, **kw) -> None:
    values = {
        "id": ticket_id,
        "portfolio_id": "pf_default",
        "tick_id": None,
        "as_of": "2026-03-17",
        "client_id": client_id,
        "ticker": "A.US",
        "side": "buy",
        "quantity": 1.0,
        "order_json": "{}",
        "status": "awaiting_approval",
        "submit_after": NOW,
        "expires_at": NOW,
        "created_at": NOW,
        "updated_at": NOW,
    } | kw
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    state.execute(f"INSERT INTO order_tickets ({cols}) VALUES ({marks})", list(values.values()))


def test_order_tickets_are_one_per_client_id_and_checked(state):
    _ticket(state, "tkt_1", "c1")
    with pytest.raises(sqlite3.IntegrityError):
        _ticket(state, "tkt_2", "c1")
    with pytest.raises(sqlite3.IntegrityError):
        _ticket(state, "tkt_3", "c3", status="lost")
    with pytest.raises(sqlite3.IntegrityError):
        _ticket(state, "tkt_4", "c4", hold="whim")
    with pytest.raises(sqlite3.IntegrityError):
        _ticket(state, "tkt_5", "c5", status="rejected")  # a rejection needs a reason
    _ticket(state, "tkt_6", "c6", status="rejected", decision_reason="too big")


def test_order_tickets_are_never_deleted(state):
    _ticket(state, "tkt_1", "c1")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        state.execute("DELETE FROM order_tickets")
