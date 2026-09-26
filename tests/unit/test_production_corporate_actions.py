"""Unit tests for applying corporate actions to production portfolios."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.core.corporate_actions import CorporateActions, Dividend, Split
from stonks.core.types import Portfolio
from stonks.production.corporate_actions import (
    adjust_working_orders,
    apply_corporate_actions,
    events_due,
    working_orders,
)
from stonks.store.state import SqliteState

ACTIONS = CorporateActions.from_events(
    [
        Split("X", date(2026, 3, 18), 2.0),
        Dividend("X", date(2026, 3, 18), 0.5),
        Dividend("X", date(2026, 3, 10), 9.0),  # on/before the snapshot
        Split("X", date(2026, 3, 25), 3.0),  # after as_of
        Dividend("Y", date(2026, 3, 19), 1.0),
    ]
)


def test_events_due_window_is_since_exclusive_as_of_inclusive():
    due = events_due(ACTIONS, since=date(2026, 3, 10), as_of=date(2026, 3, 19))
    assert due == [
        Split("X", date(2026, 3, 18), 2.0),
        Dividend("X", date(2026, 3, 18), 0.5),
        Dividend("Y", date(2026, 3, 19), 1.0),
    ]


def test_nothing_due_without_a_dated_snapshot_or_for_a_past_as_of():
    assert events_due(ACTIONS, since=None, as_of=date(2026, 3, 30)) == []
    assert events_due(ACTIONS, since=date(2026, 3, 20), as_of=date(2026, 3, 20)) == []
    assert events_due(ACTIONS, since=date(2026, 3, 20), as_of=date(2026, 3, 1)) == []


def test_split_then_dividend_on_post_split_quantity_net_of_withholding():
    p = Portfolio(cash=100.0, positions={"X": 10.0})
    records = apply_corporate_actions(
        p, ACTIONS, since=date(2026, 3, 10), as_of=date(2026, 3, 20), withholding_rate=0.2
    )
    assert p.positions == {"X": 20.0}
    assert p.cash == pytest.approx(100.0 + 20 * 0.5 * 0.8)
    assert [r.kind for r in records] == ["split", "dividend"]  # Y not held: no record


def test_consecutive_windows_apply_each_event_once():
    """Consecutive windows (S, D1] then (D1, D2] cover each event once."""
    p = Portfolio(cash=0.0, positions={"X": 1.0})
    apply_corporate_actions(p, ACTIONS, since=date(2026, 3, 10), as_of=date(2026, 3, 20))
    apply_corporate_actions(p, ACTIONS, since=date(2026, 3, 20), as_of=date(2026, 3, 31))
    assert p.positions == {"X": 6.0}
    assert p.cash == pytest.approx(1.0)


@pytest.fixture
def state(tmp_path):
    s = SqliteState(tmp_path / "state.sqlite")
    s.migrate()
    s.execute("INSERT INTO tick_runs (id, started_at, status) VALUES ('t0', 'x', 'ok')")
    for cid, ticker, status, limit in (
        ("a", "X", "pending", 40.0),
        ("b", "X", "filled", None),
        ("c", "Y", "pending", None),
        ("d", "X", "partially_filled", None),
    ):
        s.execute(
            "INSERT INTO orders (client_id, tick_id, ticker, side, quantity, order_type,"
            " limit_price, status, created_at, updated_at)"
            " VALUES (?, 't0', ?, 'buy', 4, 'limit', ?, ?, 'x', 'x')",
            [cid, ticker, limit, status],
        )
    yield s
    s.close()


def test_working_orders_are_rescaled_for_due_splits_only(state):
    working = working_orders(state)
    assert working == {"a": "X", "c": "Y", "d": "X"}
    ids = list(working)
    n = adjust_working_orders(
        state, ids, ACTIONS, since=date(2026, 3, 10), as_of=date(2026, 3, 20), now="now"
    )
    assert n == 2
    rows = {
        r["client_id"]: (r["quantity"], r["limit_price"])
        for r in state.sql("SELECT client_id, quantity, limit_price FROM orders")
    }
    assert rows == {"a": (8.0, 20.0), "b": (4.0, None), "c": (4.0, None), "d": (8.0, None)}


def test_orders_not_captured_before_the_tick_are_left_alone(state):
    n = adjust_working_orders(
        state, ["c"], ACTIONS, since=date(2026, 3, 10), as_of=date(2026, 3, 20), now="now"
    )
    assert n == 0
