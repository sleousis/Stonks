"""Migration 028 (roadmap 19.1, 19.4, 19.6, 19.7): live broker columns, the
wider ``risk_halts`` kinds, live allocations, gateway status and the
account rules tables."""

from __future__ import annotations

import sqlite3

import pytest

from stonks.production.halts import HALT_KINDS, clear_halt, trip_halt
from stonks.store.state import SqliteState


@pytest.fixture
def state(tmp_path):
    s = SqliteState(tmp_path / "state.sqlite")
    s.migrate()
    yield s
    s.close()


def _cols(state: SqliteState, table: str) -> set[str]:
    return {r["name"] for r in state.sql(f"PRAGMA table_info({table})")}


def test_orders_and_fills_gain_the_live_columns(state):
    assert {"broker_ref", "stop_price", "time_in_force", "outside_rth"} <= _cols(state, "orders")
    assert {"broker_exec_id", "fee_currency", "fee_fx_rate"} <= _cols(state, "fills")


def _order(state: SqliteState, client_id: str) -> None:
    state.execute(
        "INSERT INTO orders (client_id, tick_id, strategy_id, ticker, side, quantity,"
        " order_type, status, created_at, updated_at, portfolio_id)"
        " VALUES (?, NULL, NULL, 'AAPL.US', 'buy', 1, 'market', 'pending', 'x', 'x', 'pf_default')",
        [client_id],
    )


def test_an_execution_is_booked_once_per_portfolio(state):
    _order(state, "c1")
    insert = (
        "INSERT INTO fills (order_client_id, ticker, quantity, price, fee, filled_at,"
        " broker_exec_id, portfolio_id) VALUES ('c1', 'AAPL.US', 1, 10, 0, 'x', ?, 'pf_default')"
    )
    state.execute(insert, ["e1"])
    with pytest.raises(sqlite3.IntegrityError):
        state.execute(insert, ["e1"])
    state.execute(insert.replace("?", "NULL"))
    state.execute(insert.replace("?", "NULL"))  # fills without an exec id stay free


def test_time_in_force_is_checked(state):
    _order(state, "c1")
    with pytest.raises(sqlite3.IntegrityError):
        state.execute("UPDATE orders SET time_in_force = 'week' WHERE client_id = 'c1'")
    state.execute("UPDATE orders SET time_in_force = 'opg' WHERE client_id = 'c1'")


def test_risk_halts_accepts_the_new_kinds_and_keeps_its_guards(state):
    assert {"runaway", "broker_drift"} <= set(HALT_KINDS)
    halt, created = trip_halt(
        state, "runaway", reason="31 closes", actor="system", portfolio_id="pf_default"
    )
    assert created
    with pytest.raises(sqlite3.IntegrityError):
        state.execute("DELETE FROM risk_halts")
    with pytest.raises(sqlite3.IntegrityError):
        state.execute("UPDATE risk_halts SET reason = 'x'")
    with pytest.raises(sqlite3.IntegrityError):
        state.execute(
            "INSERT INTO risk_halts (kind, scope, portfolio_id, reason, tripped_by, tripped_at)"
            " VALUES ('runaway', 'portfolio', 'pf_default', 'again', 'system', 'x')"
        )
    clear_halt(state, halt.id, actor="user:usr_owner", reason="checked")


def test_risk_halt_rows_survive_the_rebuild(tmp_path):
    path = tmp_path / "old.sqlite"
    old = SqliteState(path)
    # apply up to 027 only
    from stonks.store import state as state_module

    migrations = sorted(state_module.MIGRATIONS_DIR.glob("*.sql"))
    old.con.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    for file in migrations:
        version = int(file.stem.split("_", 1)[0])
        if version >= 28:
            break
        old.con.executescript(
            f"BEGIN;\n{file.read_text(encoding='utf-8')}\n;\n"
            f"INSERT INTO schema_migrations VALUES ({version}, 'x');\nCOMMIT;"
        )
    trip_halt(old, "kill", reason="stop", actor="user:usr_owner", scope="global", halt="all")
    old.migrate()
    rows = old.sql("SELECT kind, halt, reason FROM risk_halts")
    assert [(r["kind"], r["halt"], r["reason"]) for r in rows] == [("kill", "all", "stop")]
    old.close()


def test_account_profile_refuses_shorts_on_a_cash_account(state):
    with pytest.raises(sqlite3.IntegrityError):
        state.execute(
            "INSERT INTO account_profiles (portfolio_id, jurisdiction, account_type, allow_short,"
            " updated_at, updated_by) VALUES ('pf_default', 'us', 'cash', 1, 'x', 'u')"
        )
    state.execute(
        "INSERT INTO account_profiles (portfolio_id, jurisdiction, updated_at, updated_by)"
        " VALUES ('pf_default', 'uk', 'x', 'u')"
    )
    row = state.sql("SELECT * FROM account_profiles")[0]
    assert row["account_type"] == "cash" and row["client_class"] == "retail"


def test_new_tables_exist(state):
    assert {
        "live_allocations",
        "broker_gateway_status",
        "account_profiles",
        "settlement_ledger",
        "account_restricted",
        "product_documents",
    } <= set(state.tables())
