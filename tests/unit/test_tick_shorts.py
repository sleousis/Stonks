"""Phase 16.1: the tick's ledger records an order's position effect
(migration 021), and a trading halt keeps covers but never short sales."""

from __future__ import annotations

import pytest

from stonks.core.types import Order
from stonks.production.tick import _record_order, _reduces
from stonks.store.state import SqliteState


@pytest.fixture
def state(tmp_path):
    s = SqliteState(tmp_path / "state.sqlite")
    s.migrate()
    yield s
    s.close()


def _effect(state: SqliteState, client_id: str):
    return state.sql("SELECT position_effect FROM orders WHERE client_id = ?", [client_id])[0][
        "position_effect"
    ]


def test_position_effect_is_written_for_short_book_orders(state) -> None:
    _record_order(state, Order("a:short", "X", "sell", 5.0, position_effect="open"), "filled")
    _record_order(state, Order("a:cover", "X", "buy", 5.0, position_effect="close"), "filled")
    assert _effect(state, "a:short") == "open"
    assert _effect(state, "a:cover") == "close"


def test_long_only_orders_leave_it_null(state) -> None:
    _record_order(state, Order("b:buy", "X", "buy", 5.0), "filled")
    assert _effect(state, "b:buy") is None


def test_the_column_only_takes_open_or_close(state) -> None:
    with pytest.raises(Exception, match="CHECK"):
        state.execute(
            "INSERT INTO orders (client_id, ticker, side, quantity, order_type, status,"
            " created_at, updated_at, position_effect)"
            " VALUES ('c', 'X', 'buy', 1, 'market', 'filled', 'now', 'now', 'sideways')"
        )


@pytest.mark.parametrize(
    ("side", "effect", "reduces"),
    [
        ("sell", None, True),
        ("buy", None, False),
        ("sell", "close", True),
        ("buy", "close", True),
        ("sell", "open", False),
        ("buy", "open", False),
    ],
)
def test_a_halt_keeps_only_position_reducing_orders(side, effect, reduces) -> None:
    assert _reduces(Order("x", "X", side, 1.0, position_effect=effect)) is reduces
