"""Migration 036 (roadmap 19.10): ``orders.oca_group`` and
``orders.protective`` for the broker-side protective stops."""

from __future__ import annotations

import sqlite3

import pytest

from stonks.store.state import SqliteState

NOW = "2026-03-17T20:00:00+00:00"


@pytest.fixture
def state(tmp_path):
    s = SqliteState(tmp_path / "state.sqlite")
    s.migrate()
    yield s
    s.close()


def _insert(state: SqliteState, client_id: str, **extra) -> None:
    cols = ["client_id", "ticker", "side", "quantity", "order_type", "status",
            "created_at", "updated_at", *extra]  # fmt: skip
    values = [client_id, "AAPL.US", "sell", 10, "stop", "pending", NOW, NOW, *extra.values()]
    state.execute(
        f"INSERT INTO orders ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", values
    )


def test_orders_gain_the_stop_columns_with_safe_defaults(state):
    _insert(state, "o1")
    [row] = state.sql("SELECT oca_group, protective FROM orders WHERE client_id = 'o1'")
    assert (row["oca_group"], row["protective"]) == (None, 0)
    _insert(state, "o2", oca_group="stk-oca-1", protective=1, stop_price=90.0,
            time_in_force="gtc")  # fmt: skip
    [row] = state.sql("SELECT oca_group, protective FROM orders WHERE client_id = 'o2'")
    assert (row["oca_group"], row["protective"]) == ("stk-oca-1", 1)


def test_protective_is_a_flag(state):
    with pytest.raises(sqlite3.IntegrityError):
        _insert(state, "o3", protective=2)
