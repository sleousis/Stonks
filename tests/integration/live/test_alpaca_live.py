"""Live contract test against Alpaca's PAPER endpoint.

Gated behind STONKS_RUN_LIVE_TESTS=1 plus ALPACA_API_KEY / ALPACA_SECRET_KEY
so the default pytest run is hermetic. Always connects with paper=True; the
order round-trip places a far-from-market limit buy and cancels it.
"""

from __future__ import annotations

import os
import uuid

import pytest

from stonks.core.types import Order
from stonks.execution.brokers import AlpacaBroker

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("STONKS_RUN_LIVE_TESTS") != "1",
        reason="set STONKS_RUN_LIVE_TESTS=1 to enable live API tests",
    ),
    pytest.mark.skipif(
        not (os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_SECRET_KEY")),
        reason="ALPACA_API_KEY / ALPACA_SECRET_KEY not set",
    ),
]


@pytest.fixture
def broker() -> AlpacaBroker:
    b = AlpacaBroker.connect(
        os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"], paper=True
    )
    assert b.paper is True
    return b


def test_live_account_portfolio_and_clock(broker):
    account = broker.fetch_account()
    assert account.currency
    portfolio = broker.fetch_portfolio()
    assert portfolio.cash == pytest.approx(account.cash)
    clock = broker.get_market_clock()
    assert clock.next_open.tzinfo is not None


def test_live_unknown_client_id_is_none(broker):
    assert broker.get_order_state(f"stonks-live-missing-{uuid.uuid4().hex}") is None


def test_live_order_idempotency_and_cancel(broker):
    client_id = f"stonks-live-{uuid.uuid4().hex}"
    order = Order(
        client_id=client_id,
        ticker="AAPL.US",
        side="buy",
        quantity=1,
        order_type="limit",
        limit_price=1.00,  # far below market: stays open
    )
    try:
        assert broker.place_order(order) is None
        # Resubmitting the same client_id must not create a second order.
        assert broker.place_order(order) is None
        state = broker.get_order_state(client_id)
        assert state is not None
        assert state.status in ("pending", "partially_filled")
        assert sum(o.client_id == client_id for o in broker.list_open_orders()) == 1
    finally:
        broker.cancel_order(client_id)
