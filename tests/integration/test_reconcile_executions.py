"""Booking fills from broker executions (roadmap 19.1): one fill per
execution id, a late commission updates the fee, and the order's status
comes from the broker while the fills come only from executions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from stonks.core.clock import FixedClock
from stonks.core.types import Portfolio
from stonks.execution.brokers.base import BrokerOrderState, Execution
from stonks.execution.reconcile import book_executions, reconcile_orders

T0 = datetime(2026, 9, 28, 13, 30, tzinfo=UTC)


def insert_order(state, client_id, *, qty=10.0, portfolio_id="pf_default", side="buy"):
    state.execute(
        "INSERT INTO orders (client_id, tick_id, strategy_id, ticker, side, quantity, order_type,"
        " limit_price, status, broker_order_id, created_at, updated_at, portfolio_id)"
        " VALUES (?, NULL, NULL, 'AAPL.US', ?, ?, 'limit', 100, 'pending', NULL, 'x', 'x', ?)",
        [client_id, side, qty, portfolio_id],
    )


def execution(exec_id, client_id="c1", qty=4.0, price=100.0, commission=None, ccy=None):
    return Execution(
        broker_exec_id=exec_id,
        client_id=client_id,
        ticker="AAPL.US",
        side="buy",
        quantity=qty,
        price=price,
        executed_at=T0,
        commission=commission,
        commission_currency=ccy,
    )


def fills(state):
    return state.sql(
        "SELECT broker_exec_id, quantity, price, fee, fee_currency, portfolio_id FROM fills"
        " ORDER BY id"
    )


def status(state, client_id="c1"):
    return state.sql("SELECT status FROM orders WHERE client_id = ?", [client_id])[0]["status"]


def test_each_execution_is_booked_once(state):
    insert_order(state, "c1")
    first = book_executions(
        state, [execution("e1"), execution("e2", qty=6.0)], clock=FixedClock(T0)
    )
    assert first.fills_inserted == 2
    again = book_executions(
        state, [execution("e1"), execution("e2", qty=6.0)], clock=FixedClock(T0)
    )
    assert again.fills_inserted == 0
    rows = fills(state)
    assert [(r["broker_exec_id"], r["quantity"]) for r in rows] == [("e1", 4.0), ("e2", 6.0)]
    assert all(r["portfolio_id"] == "pf_default" for r in rows)
    assert status(state) == "filled"


def test_a_partial_execution_marks_the_order_partially_filled(state):
    insert_order(state, "c1", qty=10.0)
    book_executions(state, [execution("e1", qty=4.0)], clock=FixedClock(T0))
    assert status(state) == "partially_filled"


def test_a_late_commission_updates_the_fee(state):
    insert_order(state, "c1")
    book_executions(state, [execution("e1")], clock=FixedClock(T0))
    assert fills(state)[0]["fee"] == 0.0
    later = book_executions(
        state, [execution("e1", commission=1.05, ccy="USD")], clock=FixedClock(T0)
    )
    assert later.fees_updated == 1 and later.fills_inserted == 0
    row = fills(state)[0]
    assert row["fee"] == 1.05 and row["fee_currency"] == "USD"
    # the same commission again changes nothing
    assert book_executions(state, [execution("e1", commission=1.05, ccy="USD")]).fees_updated == 0


def test_an_execution_of_another_portfolio_or_unknown_order_is_an_orphan(state):
    state.execute(
        "INSERT INTO portfolios (id, owner_id, name, kind, created_at)"
        " SELECT 'pf_other', owner_id, 'Other', kind, created_at FROM portfolios"
        " WHERE id = 'pf_default'"
    )
    insert_order(state, "c2", portfolio_id="pf_other")
    result = book_executions(
        state, [execution("e1", client_id="c2"), execution("e2", client_id="nope")]
    )
    assert result.fills_inserted == 0
    assert result.orphan_executions == ("e1", "e2")
    assert fills(state) == []


class ExecBroker:
    """A broker with executions and order state (IbkrBroker's shape)."""

    def __init__(self) -> None:
        self.execs: list[Execution] = []
        self.states: dict[str, BrokerOrderState] = {}
        self.since: list[datetime] = []

    def executions(self, since):
        self.since.append(since)
        return list(self.execs)

    def get_order_state(self, client_id):
        return self.states.get(client_id)

    def fetch_portfolio(self):
        return Portfolio(cash=0.0)

    def place_order(self, order):
        return None

    def reconcile(self):
        raise AssertionError("execution brokers are reconciled by executions")


def test_reconcile_orders_books_executions_and_takes_status_from_the_broker(state):
    insert_order(state, "c1", qty=10.0)
    broker = ExecBroker()
    broker.execs = [execution("e1", qty=10.0, commission=1.0, ccy="USD")]
    broker.states["c1"] = BrokerOrderState(
        client_id="c1",
        broker_order_id="perm-1",
        ticker="AAPL.US",
        side="buy",
        status="filled",
        quantity=10.0,
        filled_quantity=10.0,
        avg_fill_price=100.0,
        updated_at=T0,
    )
    summary = reconcile_orders(broker, state, now=T0)
    assert summary.fills_inserted == 1
    # no second, cumulative fill booked from the order state
    assert len(fills(state)) == 1
    row = state.sql("SELECT status, broker_order_id FROM orders WHERE client_id = 'c1'")[0]
    assert row["status"] == "filled" and row["broker_order_id"] == "perm-1"
    assert broker.since == [T0 - timedelta(days=1)]
    # idempotent
    assert reconcile_orders(broker, state, now=T0).fills_inserted == 0
