"""Migration 010_accounts: new tables, scoping columns and the single-owner
backfill of an existing (pre-010) database."""

from __future__ import annotations

import json
import sqlite3

import pytest

from stonks.store.state import SqliteState
from tests.integration.accounts.legacy import (
    ACCOUNTS_VERSION,
    legacy_state,
    migrate_to_current,
)

NOW = "2026-01-02T15:00:00+00:00"


def _seed_legacy_rows(state: SqliteState) -> None:
    """A populated pre-010 database: strategies in every status, a tick with
    orders, fills and a snapshot, a job, a draft and an alert."""
    ex = state.execute
    for sid, status in [("s_active", "active"), ("s_active2", "active"), ("s_shadow", "shadow")]:
        ex(
            "INSERT INTO strategies (id, class_path, params_json, status, created_at, updated_at)"
            " VALUES (?, 'm:C', '{}', 'shadow', ?, ?)",
            [sid, NOW, NOW],
        )
        if status == "active":
            ex(
                "INSERT INTO status_changes (strategy_id, from_status, to_status, actor, reason,"
                " override, created_at) VALUES (?, 'shadow', 'active', 't', 'seed', 1, ?)",
                [sid, NOW],
            )
            ex(
                "UPDATE strategies SET status='active', updated_at=? WHERE id=?",
                [NOW, sid],
            )
    ex("INSERT INTO tick_runs (id, started_at, status) VALUES ('t1', ?, 'ok')", [NOW])
    for cid in ("c1", "c2"):
        ex(
            "INSERT INTO orders (client_id, tick_id, strategy_id, ticker, side, quantity,"
            " order_type, status, created_at, updated_at)"
            " VALUES (?, 't1', 's_active', 'UP.US', 'buy', 1, 'market', 'filled', ?, ?)",
            [cid, NOW, NOW],
        )
        ex(
            "INSERT INTO fills (order_client_id, ticker, quantity, price, filled_at)"
            " VALUES (?, 'UP.US', 1, 100, ?)",
            [cid, NOW],
        )
    ex(
        "INSERT INTO portfolio_snapshots (tick_id, as_of, taken_at, cash, positions_json,"
        " total_value) VALUES ('t1', '2026-01-02', ?, 9800, '{\"UP.US\": 2}', 10000)",
        [NOW],
    )
    ex(
        "INSERT INTO jobs (id, kind, params_json, status, created_at)"
        " VALUES ('j1', 'backtest', '{}', 'succeeded', ?)",
        [NOW],
    )
    ex(
        "INSERT INTO strategy_drafts (id, name, kind, created_at, updated_at)"
        " VALUES ('d1', 'draft', 'rule', ?, ?)",
        [NOW, NOW],
    )
    ex(
        "INSERT INTO alerts (level, title, message, created_at) VALUES ('info', 't', 'm', ?)",
        [NOW],
    )


@pytest.fixture
def populated(tmp_path, monkeypatch):
    state = legacy_state(tmp_path / "state.sqlite", monkeypatch)
    _seed_legacy_rows(state)
    migrate_to_current(state)
    yield state
    state.close()


def _columns(state: SqliteState, table: str) -> set[str]:
    return {r["name"] for r in state.sql(f"PRAGMA table_info({table})")}


def test_accounts_migration_is_the_next_free_number(tmp_path):
    with SqliteState(tmp_path / "s.sqlite") as s:
        s.migrate()
        assert ACCOUNTS_VERSION in s.applied_migrations()
        assert ACCOUNTS_VERSION == 10


def test_new_tables_and_scoping_columns_exist(tmp_path):
    with SqliteState(tmp_path / "s.sqlite") as s:
        s.migrate()
        assert {"users", "portfolios", "subscriptions", "audit_log"} <= set(s.tables())
        for table in ("orders", "fills", "portfolio_snapshots"):
            assert "portfolio_id" in _columns(s, table)
        for table in ("jobs", "strategy_drafts"):
            assert "owner_id" in _columns(s, table)
        assert {"user_id", "category", "dedupe_key", "read_at"} <= _columns(s, "alerts")
        # S2 fills these in; the columns exist from the start.
        assert {"password_hash", "totp_secret_enc", "kind", "timezone"} <= _columns(s, "users")


def test_fresh_database_gets_default_owner_and_portfolio(tmp_path):
    with SqliteState(tmp_path / "s.sqlite") as s:
        s.migrate()
        (owner,) = s.sql("SELECT * FROM users")
        assert owner["id"] == "usr_owner"
        assert owner["role"] == "admin"
        assert owner["kind"] == "human"
        assert owner["status"] == "active"
        assert owner["password_hash"] is None
        (pf,) = s.sql("SELECT * FROM portfolios")
        assert pf["id"] == "pf_default"
        assert pf["owner_id"] == "usr_owner"
        assert pf["kind"] == "simulated"
        assert pf["allow_short"] == 0
        assert pf["initial_cash"] is None  # NULL = [production].initial_cash
        assert s.sql("SELECT COUNT(*) FROM subscriptions")[0][0] == 0


def test_backfill_assigns_every_existing_row_to_the_default_owner(populated):
    s = populated
    for table in ("orders", "fills", "portfolio_snapshots"):
        rows = s.sql(f"SELECT portfolio_id FROM {table}")
        assert rows, table
        assert {r[0] for r in rows} == {"pf_default"}, table
    for table in ("jobs", "strategy_drafts"):
        assert {r[0] for r in s.sql(f"SELECT owner_id FROM {table}")} == {"usr_owner"}
    # Alerts keep NULL = audience "admins".
    assert {r[0] for r in s.sql("SELECT user_id FROM alerts")} == {None}


def test_backfill_subscribes_default_portfolio_to_each_active_strategy(populated):
    rows = populated.sql("SELECT * FROM subscriptions ORDER BY strategy_id")
    assert [r["strategy_id"] for r in rows] == ["s_active", "s_active2"]
    for r in rows:
        assert r["user_id"] == "usr_owner"
        assert r["portfolio_id"] == "pf_default"
        assert r["mode"] == "paper"
        assert r["weight"] == 1.0
        assert r["enabled"] == 1
        assert json.loads(r["risk_overrides_json"]) == {}
        assert r["paper_days_completed"] == 0
    assert len({r["id"] for r in rows}) == 2


def test_backfill_keeps_existing_rows_otherwise_intact(populated):
    (snap,) = populated.sql("SELECT cash, positions_json, total_value FROM portfolio_snapshots")
    assert (snap[0], json.loads(snap[1]), snap[2]) == (9800, {"UP.US": 2}, 10000)
    assert populated.sql("SELECT COUNT(*) FROM orders")[0][0] == 2
    assert populated.sql("SELECT COUNT(*) FROM fills")[0][0] == 2


def test_migrate_twice_is_a_noop(populated):
    before = {t: populated.count_rows(t) for t in populated.tables()}
    populated.migrate()
    after = {t: populated.count_rows(t) for t in populated.tables()}
    assert before == after


def test_legacy_inserts_without_portfolio_id_land_in_the_sole_portfolio(populated):
    """Today's writers (tick, reconcile) don't know the column yet: while the
    deployment has exactly one portfolio their rows go to it."""
    s = populated
    s.execute(
        "INSERT INTO orders (client_id, tick_id, strategy_id, ticker, side, quantity,"
        " order_type, status, created_at, updated_at)"
        " VALUES ('c3', 't1', 's_active', 'UP.US', 'sell', 1, 'market', 'filled', ?, ?)",
        [NOW, NOW],
    )
    s.execute(
        "INSERT INTO fills (order_client_id, ticker, quantity, price, filled_at)"
        " VALUES ('c3', 'UP.US', 1, 101, ?)",
        [NOW],
    )
    s.execute(
        "INSERT INTO portfolio_snapshots (tick_id, as_of, taken_at, cash, positions_json,"
        " total_value) VALUES ('t1', '2026-01-03', ?, 1, '{}', 1)",
        [NOW],
    )
    s.execute(
        "INSERT INTO jobs (id, kind, params_json, status, created_at)"
        " VALUES ('j2', 'tick', '{}', 'queued', ?)",
        [NOW],
    )
    assert s.sql("SELECT portfolio_id FROM orders WHERE client_id='c3'")[0][0] == "pf_default"
    assert s.sql("SELECT portfolio_id FROM fills WHERE order_client_id='c3'")[0][0] == (
        "pf_default"
    )
    assert s.sql("SELECT portfolio_id FROM portfolio_snapshots ORDER BY id DESC")[0][0] == (
        "pf_default"
    )
    assert s.sql("SELECT owner_id FROM jobs WHERE id='j2'")[0][0] == "usr_owner"


def _second_portfolio(s: SqliteState) -> None:
    s.execute(
        "INSERT INTO users (id, display_name, role, created_at) VALUES ('usr_b', 'B', 'trader', ?)",
        [NOW],
    )
    s.execute(
        "INSERT INTO portfolios (id, owner_id, name, kind, created_at)"
        " VALUES ('pf_b', 'usr_b', 'B book', 'simulated', ?)",
        [NOW],
    )


def test_unscoped_insert_is_refused_once_a_second_portfolio_exists(populated):
    s = populated
    _second_portfolio(s)
    with pytest.raises(sqlite3.IntegrityError, match="portfolio_id is required"):
        s.execute(
            "INSERT INTO orders (client_id, ticker, side, quantity, order_type, status,"
            " created_at, updated_at) VALUES ('c9', 'X', 'buy', 1, 'market', 'filled', ?, ?)",
            [NOW, NOW],
        )
    with pytest.raises(sqlite3.IntegrityError, match="portfolio_id is required"):
        s.execute(
            "INSERT INTO portfolio_snapshots (taken_at, cash, positions_json, total_value)"
            " VALUES (?, 1, '{}', 1)",
            [NOW],
        )
    # A fill inherits its order's portfolio even with several portfolios.
    s.execute(
        "INSERT INTO orders (client_id, ticker, side, quantity, order_type, status,"
        " created_at, updated_at, portfolio_id)"
        " VALUES ('cb', 'X', 'buy', 1, 'market', 'filled', ?, ?, 'pf_b')",
        [NOW, NOW],
    )
    s.execute(
        "INSERT INTO fills (order_client_id, ticker, quantity, price, filled_at)"
        " VALUES ('cb', 'X', 1, 1, ?)",
        [NOW],
    )
    assert s.sql("SELECT portfolio_id FROM fills WHERE order_client_id='cb'")[0][0] == "pf_b"


def test_fill_portfolio_must_match_its_order(populated):
    s = populated
    _second_portfolio(s)
    with pytest.raises(sqlite3.IntegrityError, match="order's portfolio"):
        s.execute(
            "INSERT INTO fills (order_client_id, ticker, quantity, price, filled_at,"
            " portfolio_id) VALUES ('c1', 'UP.US', 1, 1, ?, 'pf_b')",
            [NOW],
        )


def test_unknown_portfolio_id_is_refused(populated):
    with pytest.raises(sqlite3.IntegrityError, match="unknown portfolio"):
        populated.execute(
            "INSERT INTO portfolio_snapshots (taken_at, cash, positions_json, total_value,"
            " portfolio_id) VALUES (?, 1, '{}', 1, 'pf_nope')",
            [NOW],
        )


@pytest.mark.parametrize(
    ("table", "key"),
    [("orders", "client_id = 'c1'"), ("fills", "id = 1"), ("portfolio_snapshots", "id = 1")],
)
def test_portfolio_id_is_immutable(populated, table, key):
    _second_portfolio(populated)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        populated.execute(f"UPDATE {table} SET portfolio_id = 'pf_b' WHERE {key}")


def test_audit_log_is_append_only(populated):
    s = populated
    s.execute(
        "INSERT INTO audit_log (actor, action, target_kind, target_id, details_json, created_at)"
        " VALUES ('user:usr_owner', 'x', 'portfolio', 'pf_default', '{}', ?)",
        [NOW],
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        s.execute("UPDATE audit_log SET action = 'y'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        s.execute("DELETE FROM audit_log")


def test_subscription_user_must_own_its_portfolio(populated):
    _second_portfolio(populated)
    with pytest.raises(sqlite3.IntegrityError, match="own the portfolio"):
        populated.execute(
            "INSERT INTO subscriptions (id, user_id, strategy_id, portfolio_id, mode,"
            " created_at, updated_at) VALUES ('sx', 'usr_b', 's_shadow', 'pf_default',"
            " 'paper', ?, ?)",
            [NOW, NOW],
        )


def test_paper_and_auto_need_a_portfolio(populated):
    with pytest.raises(sqlite3.IntegrityError):
        populated.execute(
            "INSERT INTO subscriptions (id, user_id, strategy_id, mode, created_at, updated_at)"
            " VALUES ('sy', 'usr_owner', 's_shadow', 'paper', ?, ?)",
            [NOW, NOW],
        )
