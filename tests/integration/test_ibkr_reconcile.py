"""The IBKR adapter through reconciliation and the order state machine
(roadmap 19.2): a real state DB, ``FakeIbGateway`` for IBKR."""

from __future__ import annotations

from datetime import timedelta

import pytest

from stonks.core.clock import FakeClock
from stonks.core.types import Order
from stonks.execution.brokers.base import OrderOutcomeUnknownError
from stonks.execution.brokers.ibkr.broker import IbkrBroker
from stonks.execution.brokers.ibkr.contracts import ContractResolver, SqliteContractCache
from stonks.execution.order_state import current_state, mark_unknown, may_submit, write_state
from stonks.execution.reconcile import reconcile_orders, startup_reconcile
from tests.fakes.ib_gateway import T0, FakeIbGateway

CID = "t1-s1-AAPL.US-buy"
NOW = T0 + timedelta(hours=1)


def insert_order(state, client_id=CID):
    state.execute(
        "INSERT INTO orders (client_id, tick_id, strategy_id, ticker, side, quantity, order_type,"
        " limit_price, status, state, broker_order_id, created_at, updated_at)"
        " VALUES (?, NULL, NULL, 'AAPL.US', 'buy', 10, 'market', NULL, 'pending', 'pending',"
        " NULL, 'x', 'x')",
        [client_id],
    )


def order(client_id=CID) -> Order:
    return Order(client_id=client_id, ticker="AAPL.US", side="buy", quantity=10,
                 decision_price=200.0)  # fmt: skip


def broker_for(state, gw) -> IbkrBroker:
    resolver = ContractResolver(gw, cache=SqliteContractCache(state), clock=FakeClock(T0))
    return IbkrBroker(gw, mode="paper", resolver=resolver, clock=FakeClock(NOW))


def fills(state):
    return state.sql("SELECT quantity, price, fee, broker_exec_id FROM fills ORDER BY id")


def test_fills_come_from_executions_and_late_commissions_update_the_fee(state):
    gw = FakeIbGateway()
    broker = broker_for(state, gw)
    insert_order(state)
    broker.place_order(order())
    write_state(state, CID, "submitted")

    first = gw.fill(CID, 4, 200.0)
    summary = reconcile_orders(broker, state, now=NOW)
    assert summary.fills_inserted == 1
    assert current_state(state, CID) == "partially_filled"

    gw.fill(CID, 6, 201.0, commission=0.6)
    gw.report_commission(first, 0.4)
    reconcile_orders(broker, state, now=NOW)
    rows = fills(state)
    assert [(r["quantity"], r["price"], r["fee"]) for r in rows] == [(4, 200.0, 0.4),
                                                                     (6, 201.0, 0.6)]  # fmt: skip
    assert current_state(state, CID) == "filled"
    row = state.sql("SELECT broker_order_id FROM orders WHERE client_id = ?", [CID])[0]
    assert row["broker_order_id"] == str(gw.trade(CID).perm_id)

    # a repeat books nothing
    assert reconcile_orders(broker, state, now=NOW).fills_inserted == 0


def test_timed_out_submit_waits_for_reconciliation_then_resolves(state):
    gw = FakeIbGateway()
    broker = broker_for(state, gw)
    insert_order(state)
    gw.submit_fault = "disconnect_after"
    with pytest.raises(OrderOutcomeUnknownError) as info:
        broker.place_order(order())
    mark_unknown(state, info.value.client_id, "submit timed out")
    assert not may_submit(current_state(state, CID))

    result = startup_reconcile(broker, state, clock=FakeClock(NOW))
    assert result.ok
    assert current_state(state, CID) == "accepted"
    assert gw.sent_count(CID) == 1


def test_an_order_that_never_arrived_is_rejected_and_may_be_sent_again(state):
    gw = FakeIbGateway()
    broker = broker_for(state, gw)
    insert_order(state)
    gw.submit_fault = "disconnect_before"
    with pytest.raises(OrderOutcomeUnknownError):
        broker.place_order(order())
    mark_unknown(state, CID, "submit dropped")
    result = startup_reconcile(broker, state, clock=FakeClock(NOW))
    # the gateway never saw it: reconciliation marks it rejected (not sent)
    assert result.ok
    assert current_state(state, CID) == "rejected"
    assert gw.sent_count(CID) == 0


def test_contract_cache_is_shared_across_processes(state):
    gw = FakeIbGateway()
    broker_for(state, gw).place_order(order())
    broker_for(state, gw).place_order(order("t1-s1-AAPL.US-buy-2"))
    assert len(gw.lookups) == 1
