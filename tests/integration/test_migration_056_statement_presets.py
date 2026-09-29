"""Migration 056: statement imports record their broker preset and kind,
and a holdings export keeps its lines in ``statement_import_holdings``."""

from __future__ import annotations

import sqlite3

import pytest

from stonks.store.state import SqliteState

NOW = "2026-09-29T06:00:00+00:00"


@pytest.fixture
def state(tmp_path):
    s = SqliteState(tmp_path / "state.sqlite")
    s.migrate()
    yield s
    s.close()


def _import(state: SqliteState, import_id: str, kind: str | None = None) -> None:
    state.execute(
        "INSERT INTO broker_connections (id, user_id, provider, status, created_at, updated_at)"
        " VALUES ('con_1', 'usr_owner', 'csv', 'active', ?, ?) ON CONFLICT DO NOTHING",
        [NOW, NOW],
    )
    cols = "id, user_id, portfolio_id, connection_id, mapping_json, rows_total, rows_added,"
    cols += " rows_duplicate, rows_skipped, created_at"
    values = [import_id, "usr_owner", "pf_default", "con_1", "{}", 1, 1, 0, 0, NOW]
    if kind is not None:
        cols += ", kind, preset, as_of"
        values += [kind, "degiro_portfolio", "2025-06-10"]
    marks = ", ".join("?" * len(values))
    state.execute(f"INSERT INTO statement_imports ({cols}) VALUES ({marks})", values)


def test_an_import_defaults_to_activities(state):
    _import(state, "imp_1")
    row = state.sql("SELECT kind, preset, as_of FROM statement_imports WHERE id = 'imp_1'")[0]
    assert (row["kind"], row["preset"], row["as_of"]) == ("activities", None, None)


def test_holdings_lines_belong_to_their_import(state):
    _import(state, "imp_2", kind="holdings")
    state.execute(
        "INSERT INTO statement_import_holdings (import_id, row_id, raw_symbol, ticker, quantity,"
        " price, market_value, currency, is_cash) VALUES"
        " ('imp_2', 'r1', 'US0378331005', 'AAPL.US', 6, 229.35, 1376.1, 'USD', 0),"
        " ('imp_2', 'r2', 'CASH:EUR', NULL, 1543.21, 1, 1543.21, 'EUR', 1)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        state.execute(
            "INSERT INTO statement_import_holdings (import_id, row_id, raw_symbol, quantity)"
            " VALUES ('imp_2', 'r1', 'X', 1)"
        )
    with pytest.raises(sqlite3.IntegrityError):
        _import(state, "imp_3", kind="positions")
    state.execute("PRAGMA foreign_keys = ON")
    state.execute("DELETE FROM statement_imports WHERE id = 'imp_2'")
    assert state.sql("SELECT count(*) FROM statement_import_holdings")[0][0] == 0
