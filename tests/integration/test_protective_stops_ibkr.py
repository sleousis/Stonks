"""Protective stops at IBKR (roadmap 19.10): a real state DB, the IBKR
adapter and ``FakeIbGateway``. A stop goes out good till cancelled in its
OCA group after the entry filled, is kept while the position stands, is
cancelled and replaced when the position shrinks, and its fill comes back
through reconciliation as the strategy's closing fill and a stop-out."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from stonks.core.clock import FakeClock
from stonks.core.types import Order
from stonks.execution.brokers.ibkr.broker import IbkrBroker
from stonks.execution.brokers.ibkr.contracts import ContractResolver, SqliteContractCache
from stonks.execution.order_state import current_state, write_state
from stonks.execution.reconcile import reconcile_orders
from stonks.production.live.stops import (
    load_working_stops,
    oca_group_for,
    plan_book,
    send_stop_plan,
    tag_exits,
)
from stonks.production.live.trades import closed_trades
from stonks.production.rules._stop_settings import ProtectiveStopSettings
from stonks.production.tick import _record_order
from tests.fakes.ib_gateway import T0, FakeIbGateway

PF = "pf_default"
AS_OF = date(2026, 3, 17)
NOW = T0 + timedelta(hours=1)
ON = ProtectiveStopSettings(enabled=True)
ENTRY = "2026-03-16:pf_default:s1:AAPL.US:buy"


def broker_for(state, gw) -> IbkrBroker:
    resolver = ContractResolver(gw, cache=SqliteContractCache(state), clock=FakeClock(T0))
    return IbkrBroker(gw, mode="paper", resolver=resolver, clock=FakeClock(NOW))


def enter(state, gw, broker, client_id=ENTRY, side="buy", qty=10.0, price=200.0,
          effect="open") -> None:  # fmt: skip
    order = Order(client_id=client_id, ticker="AAPL.US", side=side, quantity=qty,
                  decision_price=price, strategy_id="s1", position_effect=effect)  # fmt: skip
    _record_order(state, order, status="pending", portfolio_id=PF)
    broker.place_order(order)
    write_state(state, client_id, "submitted")
    gw.fill(broker.broker_ref(client_id), qty, price)
    reconcile_orders(broker, state, now=NOW, portfolio_id=PF)


def sync(state, broker, positions):
    plan = plan_book(state, None, portfolio_id=PF, positions=positions,
                     settings_for=lambda sid: ON, as_of=AS_OF, tick_id=None)  # fmt: skip
    return plan, send_stop_plan(state, broker, plan, portfolio_id=PF, clock=FakeClock(NOW))


def stop_request(gw, ref):
    return next(r for _, r in gw.sent if r.order_ref == ref)


@pytest.fixture
def world(state):
    state.execute(
        "INSERT INTO strategies (id, class_path, params_json, artifact_path, status, created_at,"
        " updated_at) VALUES ('s1', 'x.Y', '{}', 'a', 'active', 'x', 'x')"
    )
    gw = FakeIbGateway()
    broker = broker_for(state, gw)
    return state, gw, broker


def test_the_entry_fill_gets_a_gtc_stop_in_its_oca_group(world):
    state, gw, broker = world
    enter(state, gw, broker)
    _, done = sync(state, broker, {"AAPL.US": 10.0})
    stop_id = f"{ENTRY}:stop"
    assert done.placed == (stop_id,)
    req = stop_request(gw, broker.broker_ref(stop_id))
    assert (req.action, req.order_type, req.tif, req.total_quantity) == ("SELL", "STP", "GTC", 10)
    assert req.aux_price == pytest.approx(180.0)  # 10% fallback without an ATR
    assert req.oca_group == oca_group_for(PF, "AAPL.US", ENTRY) and req.oca_type == 2
    [row] = state.sql("SELECT * FROM orders WHERE client_id = ?", [stop_id])
    assert (row["protective"], row["time_in_force"], row["stop_price"]) == (1, "gtc", 180.0)
    assert (row["strategy_id"], row["position_effect"], row["state"]) == ("s1", "close",
                                                                          "accepted")  # fmt: skip

    # a second sync keeps it and sends nothing
    plan, again = sync(state, broker, {"AAPL.US": 10.0})
    assert again.placed == () and again.kept == 1
    assert gw.sent_count(broker.broker_ref(stop_id)) == 1


def test_a_smaller_position_cancels_and_replaces_the_stop(world):
    state, gw, broker = world
    enter(state, gw, broker)
    sync(state, broker, {"AAPL.US": 10.0})
    enter(state, gw, broker, client_id="x1", side="sell", qty=4.0, price=210.0, effect="close")
    _, done = sync(state, broker, {"AAPL.US": 6.0})
    old, new = f"{ENTRY}:stop", f"{ENTRY}:stop:2"
    assert done.cancelled == (old,) and done.placed == (new,)
    assert current_state(state, old) == "cancelled"
    [row] = state.sql("SELECT status_reason FROM orders WHERE client_id = ?", [old])
    assert row["status_reason"] == "replaced by a stop for the new position size"
    req = stop_request(gw, broker.broker_ref(new))
    assert (req.total_quantity, req.aux_price) == (6, pytest.approx(180.0))
    assert [s.client_id for s in load_working_stops(state, PF)] == [new]


def test_a_stop_fill_is_a_closing_fill_and_a_stop_out(world):
    state, gw, broker = world
    enter(state, gw, broker)
    sync(state, broker, {"AAPL.US": 10.0})
    stop_id = f"{ENTRY}:stop"
    gw.trigger_stop(broker.broker_ref(stop_id), 179.5)
    reconcile_orders(broker, state, now=NOW, portfolio_id=PF)
    assert current_state(state, stop_id) == "filled"
    [trade] = closed_trades(state, PF, NOW.date())
    assert (trade.strategy_id, trade.stop, trade.loss) == ("s1", True, True)
    # the position is gone: nothing left to protect or cancel
    plan, done = sync(state, broker, {})
    assert plan.empty and done.placed == () and done.cancelled == ()


def test_an_exit_in_the_stop_group_shrinks_the_stop_at_the_broker(world):
    state, gw, broker = world
    enter(state, gw, broker)
    sync(state, broker, {"AAPL.US": 10.0})
    [stop] = load_working_stops(state, PF)
    exit_order = Order(client_id="x2", ticker="AAPL.US", side="sell", quantity=10.0,
                       decision_price=205.0, strategy_id="s1", position_effect="close")  # fmt: skip
    [tagged] = tag_exits([exit_order], [stop])
    assert tagged.oca_group == stop.oca_group
    _record_order(state, tagged, status="pending", portfolio_id=PF)
    broker.place_order(tagged)
    write_state(state, "x2", "submitted")
    gw.fill("x2", 10, 205.0)
    reconcile_orders(broker, state, now=NOW, portfolio_id=PF)
    assert current_state(state, stop.client_id) == "cancelled"
    assert current_state(state, "x2") == "filled"


def test_a_closed_position_cancels_its_stop(world):
    state, gw, broker = world
    enter(state, gw, broker)
    sync(state, broker, {"AAPL.US": 10.0})
    _, done = sync(state, broker, {})
    assert done.cancelled == (f"{ENTRY}:stop",)
    assert gw.trade(broker.broker_ref(f"{ENTRY}:stop")).status == "Cancelled"
    [row] = state.sql("SELECT status_reason FROM orders WHERE client_id = ?", [f"{ENTRY}:stop"])
    assert row["status_reason"] == "the position closed"


def test_the_owner_positions_in_a_shared_account_get_no_stop(world):
    state, gw, broker = world
    enter(state, gw, broker)
    # the account holds 50 more AAPL and some MSFT the owner bought by hand;
    # the book's own view is what its fills explain
    plan, done = sync(state, broker, {"AAPL.US": 10.0, "MSFT.US": 30.0})
    assert [o.ticker for o in plan.place] == ["AAPL.US"] and plan.place[0].quantity == 10.0
