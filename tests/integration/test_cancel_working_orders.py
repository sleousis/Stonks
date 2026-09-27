"""execution.cancel: the kill switch cancels orders still working at an
external broker, through the broker interface, and books the outcome by
reconciling each order (TO-08)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from stonks.execution.brokers.base import OrderCanceller
from stonks.execution.cancel import CANCEL_REASON, cancel_working_orders
from stonks.store.state import SqliteState
from tests.integration.test_reconcile import (
    FakeStateBroker,
    fills_for,
    insert_order,
    order_row,
)


class CancellingBroker(FakeStateBroker):
    """A broker with an order book that can cancel working orders."""

    def __init__(self) -> None:
        super().__init__()
        self.cancelled: list[str] = []
        self.cancel_errors: set[str] = set()

    def cancel_order(self, client_id: str) -> bool:
        if client_id in self.cancel_errors:
            raise RuntimeError("broker down")
        order = self.book.get(client_id)
        if order is None or order.status not in ("pending", "partially_filled"):
            return False
        self.cancelled.append(client_id)
        self.book[client_id] = replace(order, status="cancelled")
        return True


@pytest.fixture
def state(tmp_path):
    s = SqliteState(tmp_path / "state.sqlite")
    s.migrate()
    yield s
    s.close()


def _pf_default(state) -> None:
    state.execute("UPDATE orders SET portfolio_id = 'pf_default'")


def test_the_capability_is_detected():
    assert isinstance(CancellingBroker(), OrderCanceller)
    assert not isinstance(FakeStateBroker(), OrderCanceller)


def test_every_working_order_is_cancelled_and_booked(state):
    broker = CancellingBroker()
    insert_order(state, "buy1", side="buy")
    insert_order(state, "sell1", side="sell")
    insert_order(state, "done", status="filled")
    _pf_default(state)
    broker.set("buy1", "pending", 0.0, None)
    broker.set("sell1", "partially_filled", 4.0, 100.0, side="sell")
    broker.set("done", "filled", 10.0, 100.0)

    summary = cancel_working_orders(broker, state, portfolio_id="pf_default")

    assert sorted(summary.cancelled) == ["buy1", "sell1"]
    assert broker.cancelled == ["buy1", "sell1"]
    assert order_row(state, "buy1")["status"] == "cancelled"
    assert order_row(state, "buy1")["status_reason"] == CANCEL_REASON
    # the partial fill made before the cancel is booked, once
    assert order_row(state, "sell1")["status"] == "cancelled"
    assert [f["quantity"] for f in fills_for(state, "sell1")] == pytest.approx([4.0])
    assert order_row(state, "done")["status"] == "filled"


def test_flatten_cancels_only_working_buys(state):
    broker = CancellingBroker()
    insert_order(state, "buy1", side="buy")
    insert_order(state, "sell1", side="sell")
    _pf_default(state)
    broker.set("buy1", "pending", 0.0, None)
    broker.set("sell1", "pending", 0.0, None, side="sell")

    summary = cancel_working_orders(broker, state, portfolio_id="pf_default", sides=("buy",))

    assert summary.cancelled == ("buy1",)
    assert order_row(state, "sell1")["status"] == "pending"


def test_other_portfolios_orders_are_left_alone(state):
    broker = CancellingBroker()
    insert_order(state, "mine")
    state.execute(
        "INSERT INTO portfolios (id, owner_id, name, kind, created_at)"
        " VALUES ('pf_other', 'usr_owner', 'Other', 'simulated', 'x')"
    )
    state.execute(
        "INSERT INTO orders (client_id, ticker, side, quantity, order_type, status,"
        " created_at, updated_at, portfolio_id)"
        " VALUES ('theirs', 'AAPL.US', 'buy', 10, 'market', 'pending', 'x', 'x', 'pf_other')"
    )
    broker.set("mine", "pending", 0.0, None)
    broker.set("theirs", "pending", 0.0, None)

    cancel_working_orders(broker, state, portfolio_id="pf_default")

    assert broker.cancelled == ["mine"]


def test_one_failed_cancel_does_not_stop_the_rest(state):
    broker = CancellingBroker()
    insert_order(state, "a")
    insert_order(state, "b")
    _pf_default(state)
    broker.set("a", "pending", 0.0, None)
    broker.set("b", "pending", 0.0, None)
    broker.cancel_errors.add("a")

    summary = cancel_working_orders(broker, state, portfolio_id="pf_default")

    assert summary.failed == ("a",) and summary.cancelled == ("b",)
    assert order_row(state, "a")["status"] == "pending"


def test_an_order_that_filled_before_the_cancel_is_booked_as_filled(state):
    broker = CancellingBroker()
    insert_order(state, "late")
    _pf_default(state)
    broker.set("late", "filled", 10.0, 100.0)

    summary = cancel_working_orders(broker, state, portfolio_id="pf_default")

    assert summary.cancelled == () and summary.not_cancelled == ("late",)
    assert order_row(state, "late")["status"] == "filled"
    assert len(fills_for(state, "late")) == 1


def test_a_broker_that_cannot_cancel_is_reported(state):
    insert_order(state, "a")
    _pf_default(state)
    summary = cancel_working_orders(FakeStateBroker(), state, portfolio_id="pf_default")
    assert summary.unsupported and summary.cancelled == ()
    assert order_row(state, "a")["status"] == "pending"


def test_a_split_adjusted_partial_fill_gets_no_fake_delta_at_reconcile(state):
    """A working order 4/10 filled before a 2:1 split: the ledger is moved
    to post-split shares (order 20, booked fill 8 at half the price), as the
    broker does, so the next reconcile books no phantom 4 shares."""
    from datetime import date

    from stonks.core.corporate_actions import Split
    from stonks.execution.reconcile import reconcile_orders
    from stonks.production.corporate_actions import adjust_orders_for_splits

    broker = CancellingBroker()
    insert_order(state, "2026-03-17:x:AAPL.US:buy", qty=10.0)
    _pf_default(state)
    broker.set("2026-03-17:x:AAPL.US:buy", "partially_filled", 4.0, 100.0)
    reconcile_orders(broker, state)
    assert [f["quantity"] for f in fills_for(state, "2026-03-17:x:AAPL.US:buy")] == [4.0]

    split = Split("AAPL.US", date(2026, 3, 18), 2.0)
    adjust_orders_for_splits(state, ["2026-03-17:x:AAPL.US:buy"], [split], now="now")
    broker.set("2026-03-17:x:AAPL.US:buy", "partially_filled", 8.0, 50.0, qty=20.0)
    summary = reconcile_orders(broker, state)

    assert summary.fills_inserted == 0
    [fill] = fills_for(state, "2026-03-17:x:AAPL.US:buy")
    assert (fill["quantity"], fill["price"]) == (8.0, 50.0)
    assert order_row(state, "2026-03-17:x:AAPL.US:buy")["quantity"] == 20.0
