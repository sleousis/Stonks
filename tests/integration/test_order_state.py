"""The order state machine against the ledger (roadmap 19.1): writes go
through the table, a timed-out order becomes ``unknown`` and blocks the
submit until reconciliation resolves it by client id, and IBKR statuses
map onto it."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from stonks.core.clock import FakeClock
from stonks.core.types import Portfolio
from stonks.execution.brokers.base import BrokerOrderState
from stonks.execution.brokers.ibkr.status import ibkr_state
from stonks.execution.order_state import (
    IllegalTransitionError,
    ReconciliationPendingError,
    current_state,
    mark_unknown,
    require_reconciled,
    unknown_orders,
    write_state,
)
from stonks.execution.reconcile import reconcile_orders, startup_reconcile

T0 = datetime(2026, 9, 28, 13, 30, tzinfo=UTC)
CLOCK = FakeClock(T0)


def insert_order(state, client_id, status="pending", portfolio_id="pf_default"):
    state.execute(
        "INSERT INTO orders (client_id, ticker, side, quantity, order_type, status,"
        " created_at, updated_at, portfolio_id) VALUES (?, 'AAPL.US', 'buy', 10, 'limit', ?,"
        " 'x', 'x', ?)",
        [client_id, status, portfolio_id],
    )


def row(state, client_id):
    return state.sql("SELECT status, state, status_reason FROM orders WHERE client_id = ?",
                     [client_id])[0]  # fmt: skip


def test_writes_follow_the_table_and_keep_status_in_step(state):
    insert_order(state, "c1")
    assert current_state(state, "c1") == "pending"  # read from status
    assert write_state(state, "c1", "submitted", clock=CLOCK)
    assert (row(state, "c1")["status"], row(state, "c1")["state"]) == ("pending", "submitted")
    assert not write_state(state, "c1", "submitted", clock=CLOCK)  # a no-op
    write_state(state, "c1", "accepted", clock=CLOCK)
    write_state(state, "c1", "expired", reason="auction did not fill", clock=CLOCK)
    got = row(state, "c1")
    assert (got["status"], got["state"], got["status_reason"]) == (
        "cancelled",
        "expired",
        "auction did not fill",
    )
    with pytest.raises(IllegalTransitionError):
        write_state(state, "c1", "filled", clock=CLOCK)
    with pytest.raises(KeyError):
        write_state(state, "nope", "filled", clock=CLOCK)
    assert current_state(state, "nope") is None


def test_a_timed_out_order_blocks_the_submit_until_reconciled(state):
    insert_order(state, "c1")
    write_state(state, "c1", "submitted", clock=CLOCK)
    assert mark_unknown(state, "c1", "submit timed out", clock=CLOCK)
    assert row(state, "c1")["status"] == "pending"  # still open for every reader
    assert unknown_orders(state, "pf_default") == ("c1",)
    with pytest.raises(ReconciliationPendingError) as err:
        require_reconciled(state, "pf_default")
    assert err.value.client_ids == ("c1",)
    # a terminal order never becomes unknown
    insert_order(state, "c2", status="filled")
    assert not mark_unknown(state, "c2", "late timeout", clock=CLOCK)
    assert not mark_unknown(state, "missing", "x", clock=CLOCK)


class StateBroker:
    def __init__(self, book):
        self.book = book

    def get_order_state(self, client_id):
        return self.book.get(client_id)

    def fetch_portfolio(self):
        return Portfolio(cash=0.0)

    def place_order(self, order):
        return None

    def reconcile(self):
        raise AssertionError


def _broker_state(client_id, status, filled=0.0, state=None):
    return BrokerOrderState(
        client_id=client_id,
        broker_order_id="perm-1",
        ticker="AAPL.US",
        side="buy",
        status=status,
        quantity=10.0,
        filled_quantity=filled,
        avg_fill_price=100.0 if filled else None,
        updated_at=T0,
        state=state,
    )


def test_startup_reconciliation_resolves_unknown_orders_by_client_id(state):
    insert_order(state, "c1")
    insert_order(state, "c2")
    mark_unknown(state, "c1", "submit timed out", clock=CLOCK)
    mark_unknown(state, "c2", "submit timed out", clock=CLOCK)
    broker = StateBroker({"c1": _broker_state("c1", "pending")})
    report = startup_reconcile(broker, state, clock=CLOCK)
    # c1 is working at the broker, c2 never arrived: both resolved
    assert report.ok and report.unresolved == ()
    assert row(state, "c1")["state"] == "accepted"
    assert (row(state, "c2")["state"], row(state, "c2")["status"]) == ("rejected", "rejected")
    require_reconciled(state, "pf_default")


def test_an_illegal_broker_report_is_refused_and_reported(state):
    insert_order(state, "c1")
    write_state(state, "c1", "accepted", clock=CLOCK)
    write_state(state, "c1", "pending_cancel", clock=CLOCK)
    # the broker's fine state disagrees in a way the machine forbids
    broker = StateBroker({"c1": _broker_state("c1", "rejected", state="rejected")})
    summary = reconcile_orders(broker, state, now=T0)
    assert summary.failed_orders == ("c1",)
    assert row(state, "c1")["state"] == "pending_cancel"
    report = startup_reconcile(broker, state, clock=CLOCK)
    assert not report.ok


def test_a_fill_moves_the_state_through_the_table(state):
    insert_order(state, "c1")
    broker = StateBroker({"c1": _broker_state("c1", "partially_filled", filled=4.0)})
    reconcile_orders(broker, state, now=T0)
    assert row(state, "c1")["state"] == "partially_filled"
    broker.book["c1"] = _broker_state("c1", "filled", filled=10.0)
    reconcile_orders(broker, state, now=T0)
    assert (row(state, "c1")["state"], row(state, "c1")["status"]) == ("filled", "filled")


@pytest.mark.parametrize(
    ("status", "filled", "tif", "expected"),
    [
        ("ApiPending", 0.0, None, "pending"),
        ("PendingSubmit", 0.0, None, "submitted"),
        ("PreSubmitted", 0.0, "opg", "accepted"),
        ("Submitted", 0.0, None, "accepted"),
        ("Submitted", 3.0, None, "partially_filled"),
        ("PendingCancel", 0.0, None, "pending_cancel"),
        ("Cancelled", 0.0, None, "cancelled"),
        ("Cancelled", 0.0, "opg", "expired"),
        ("Cancelled", 2.0, "opg", "cancelled"),
        ("ApiCancelled", 0.0, None, "cancelled"),
        ("Filled", 10.0, None, "filled"),
        ("Inactive", 0.0, None, "unknown"),
        ("SomethingNew", 0.0, None, "unknown"),
    ],
)
def test_ibkr_statuses_map_onto_the_machine(status, filled, tif, expected):
    assert ibkr_state(status, filled=filled, time_in_force=tif) == expected
