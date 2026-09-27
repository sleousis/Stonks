"""Live contract tests against an IB Gateway logged in to a PAPER account
(roadmap 19.2, ``docs/design/live-trading.md`` section 8).

Gated behind ``STONKS_RUN_LIVE_TESTS=1`` plus ``STONKS_IBKR_HOST``,
``STONKS_IBKR_PORT`` and ``STONKS_IBKR_ACCOUNT``. They refuse to run unless
the account starts with ``DU`` (paper). No credential is read: the gateway
holds the login. The order round trip places a far-from-market limit buy,
finds it by ``orderRef`` and cancels it.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from stonks.core.types import Order
from stonks.execution.brokers.ibkr.broker import IbkrBroker
from stonks.execution.brokers.ibkr.client import IbContractQuery, IbEndpoint, IbOrderRequest
from stonks.execution.brokers.ibkr.contracts import ContractResolver

ACCOUNT = os.environ.get("STONKS_IBKR_ACCOUNT", "")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("STONKS_RUN_LIVE_TESTS") != "1",
        reason="set STONKS_RUN_LIVE_TESTS=1 to enable live API tests",
    ),
    pytest.mark.skipif(
        not (os.environ.get("STONKS_IBKR_HOST") and os.environ.get("STONKS_IBKR_PORT")),
        reason="STONKS_IBKR_HOST / STONKS_IBKR_PORT not set",
    ),
    pytest.mark.skipif(
        not ACCOUNT.upper().startswith("DU"),
        reason="STONKS_IBKR_ACCOUNT must be a paper (DU) account",
    ),
]


@pytest.fixture(scope="module")
def client():
    from stonks.execution.brokers.ibkr.ib_async_client import IbAsyncClient

    endpoint = IbEndpoint(
        host=os.environ["STONKS_IBKR_HOST"],
        port=int(os.environ["STONKS_IBKR_PORT"]),
        client_id=int(os.environ.get("STONKS_IBKR_CLIENT_ID", "91")),
        request_timeout=15.0,
    )
    c = IbAsyncClient(endpoint)
    c.connect()
    yield c
    c.close()


@pytest.fixture(scope="module")
def broker(client):
    return IbkrBroker(client, mode="paper", account_id=ACCOUNT)


def test_live_login_account_and_positions(broker):
    check = broker.login_check()
    assert check.ok, check.detail
    assert broker.account_id == ACCOUNT
    account = broker.fetch_account()
    assert account.equity > 0 and len(account.currency) == 3
    broker.fetch_portfolio()


def test_live_contracts_resolve(client):
    resolver = ContractResolver(client)
    for ticker in ("AAPL.US", "BRK-B.US", "VOD.LSE"):
        resolved = resolver.resolve(ticker)
        assert resolved.con_id > 0 and resolved.min_tick > 0
        assert resolved.spec().broker_id("ibkr") == str(resolved.con_id)


def test_live_order_ref_length_is_kept(client, broker):
    """The gateway keeps an orderRef of order_ref_max_length characters."""
    (details,) = client.contract_details(IbContractQuery("AAPL", "USD", primary_exchange="NASDAQ"))
    ref = ("live-ref-" + uuid.uuid4().hex * 2)[: broker.order_settings.order_ref_max_length]
    request = IbOrderRequest(action="BUY", total_quantity=1, order_type="LMT", tif="DAY",
                             order_ref=ref, account=ACCOUNT, limit_price=1.0)  # fmt: skip
    trade = client.place_order(details.contract, request)
    try:
        kept = [t.order_ref for t in client.open_trades() if t.order_id == trade.order_id]
        assert kept == [ref]
    finally:
        client.cancel_order(trade.order_id)


def test_live_what_if_does_not_send(broker):
    order = Order(client_id=f"live-wi-{uuid.uuid4().hex[:8]}", ticker="AAPL.US", side="buy",
                  quantity=1, order_type="limit", limit_price=1.0, time_in_force="day")  # fmt: skip
    preview = broker.what_if(order)
    assert preview.client_id == order.client_id
    assert broker.get_order_state(order.client_id) is None


def test_live_far_limit_round_trip(broker):
    cid = f"live-rt-{uuid.uuid4().hex[:12]}"
    order = Order(client_id=cid, ticker="AAPL.US", side="buy", quantity=1, order_type="limit",
                  limit_price=1.0, time_in_force="day")  # fmt: skip
    broker.place_order(order)
    state = None
    for _ in range(20):
        state = broker.get_order_state(cid)
        if state is not None and state.state != "submitted":
            break
        time.sleep(0.5)
    assert state is not None, "the order was not found by orderRef"
    broker.place_order(order)  # found: never sent twice
    assert broker.cancel_order(cid) is True
    broker.executions(datetime.now(UTC) - timedelta(days=1))


def test_live_quotes(broker):
    quotes = broker.quotes(["AAPL.US"])
    if quotes:
        assert quotes["AAPL.US"].reference is not None or quotes["AAPL.US"].mid is None
