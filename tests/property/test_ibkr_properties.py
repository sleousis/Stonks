"""IBKR adapter properties (roadmap 19.2, design section 8):

- an order is never sent twice for one client id across any sequence of
  retries, crashes (a fresh broker), disconnects, restarts and faults;
- the executions the broker reports, with their commissions, equal the
  gateway's executions.
"""

from __future__ import annotations

import contextlib
from datetime import timedelta

from hypothesis import given
from hypothesis import strategies as st

from stonks.core.types import Order
from stonks.execution.brokers.base import BrokerError
from stonks.execution.brokers.ibkr.broker import IbkrBroker
from tests.fakes.ib_gateway import T0, FakeIbGateway

CIDS = ["a-AAPL.US", "b-MSFT.US", "c-" + "x" * 60]
TICKERS = {"a-AAPL.US": "AAPL.US", "b-MSFT.US": "MSFT.US", CIDS[2]: "AAPL.US"}

events = st.one_of(
    st.tuples(st.just("submit"), st.sampled_from(CIDS)),
    st.tuples(st.just("crash"), st.none()),
    st.tuples(st.just("drop"), st.none()),
    st.tuples(st.just("restart"), st.none()),
    st.tuples(
        st.just("fault"),
        st.sampled_from(["disconnect_before", "disconnect_after", "timeout_after"]),
    ),
    st.tuples(st.just("connect_fails"), st.none()),
)


def _order(cid: str) -> Order:
    return Order(client_id=cid, ticker=TICKERS[cid], side="buy", quantity=5, decision_price=100.0)


@given(st.lists(events, max_size=25))
def test_an_order_is_never_sent_twice(script):
    gw = FakeIbGateway()
    broker = IbkrBroker(gw, mode="paper")
    for kind, arg in script:
        if kind == "submit":
            with contextlib.suppress(BrokerError):
                broker.place_order(_order(arg))
        elif kind == "crash":
            broker = IbkrBroker(gw, mode="paper")
        elif kind == "drop":
            gw.drop()
        elif kind == "restart":
            gw.restart()
        elif kind == "fault":
            gw.submit_fault = arg
        elif kind == "connect_fails":
            gw.connect_failures = 1
    for cid in CIDS:
        ref = broker.broker_ref(cid)
        assert gw.sent_count(ref) <= 1


@given(
    st.lists(
        st.tuples(
            st.integers(min_value=1, max_value=5),
            st.floats(min_value=1, max_value=500, allow_nan=False),
            st.one_of(st.none(), st.floats(min_value=0, max_value=5, allow_nan=False)),
        ),
        max_size=10,
    )
)
def test_booked_executions_equal_the_gateways(fills):
    gw = FakeIbGateway()
    broker = IbkrBroker(gw, mode="paper")
    broker.place_order(
        Order(client_id="c1", ticker="AAPL.US", side="buy", quantity=1000, decision_price=100.0)
    )
    for qty, price, commission in fills:
        gw.fill("c1", qty, price, commission=commission)
    got = broker.executions(T0 - timedelta(days=1))
    assert [(e.quantity, e.price, e.commission) for e in got] == fills
    assert sum(f.fee for f in broker.reconcile()) == sum(c or 0.0 for _, _, c in fills)
