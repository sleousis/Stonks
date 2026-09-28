"""Live contract tests against an IB Gateway logged in to a PAPER account
(roadmap 19.2 and 19.11, ``docs/design/live-trading.md`` section 8).

Gated behind ``STONKS_RUN_LIVE_TESTS=1`` plus ``STONKS_IBKR_HOST``,
``STONKS_IBKR_PORT`` and ``STONKS_IBKR_ACCOUNT``. They refuse to run unless
the account starts with ``DU`` (paper). No credential is read: the gateway
holds the login. The order round trip places a far-from-market limit buy,
finds it by ``orderRef`` and cancels it. Before any test runs, the module
checks that every account the gateway manages is a paper (``DU``) account
and stops the whole run otherwise, so a live login can never take an order.
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
from stonks.logging import get_logger

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
    accounts = [a for a in c.managed_accounts() if a]
    if not accounts or not all(a.upper().startswith("DU") for a in accounts):
        c.close()
        pytest.exit("the gateway is not logged in to a paper (DU) account only", returncode=2)
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


#: orderRef lengths the measurement tries (roadmap 19.16).
REF_PROBE_LENGTHS = (24, 32, 40, 48, 64, 80, 96, 128)


def _kept_ref(client, contract, ref: str) -> str | None:
    """Place a far limit buy with ``ref``, read back what the gateway kept
    on the open order and on the completed one after the cancel."""
    request = IbOrderRequest(action="BUY", total_quantity=1, order_type="LMT", tif="DAY",
                             order_ref=ref, account=ACCOUNT, limit_price=1.0)  # fmt: skip
    trade = client.place_order(contract, request)
    try:
        open_refs = [t.order_ref for t in client.open_trades() if t.perm_id == trade.perm_id]
    finally:
        client.cancel_order(trade.order_id)
    time.sleep(1.0)
    done = [t.order_ref for t in client.completed_trades() if t.perm_id == trade.perm_id]
    kept = [r for r in (*open_refs, *done) if r]
    return min(kept, key=len) if kept else None


def test_live_order_ref_max_length_is_measured(client, broker, record_property):
    """Measure the longest orderRef the gateway keeps whole, on the open
    order and after it completes. ``[brokers.ibkr.orders]
    order_ref_max_length`` must not be longer (docs/design/live-trading.md,
    section 16). The value is logged and recorded as a test property."""
    (details,) = client.contract_details(IbContractQuery("AAPL", "USD", primary_exchange="NASDAQ"))
    measured = 0
    results: dict[int, int | None] = {}
    for length in REF_PROBE_LENGTHS:
        ref = (f"len{length}-" + uuid.uuid4().hex * 4)[:length]
        kept = _kept_ref(client, details.contract, ref)
        results[length] = None if kept is None else len(kept)
        if kept != ref:
            break
        measured = length
    record_property("order_ref_kept", results)
    record_property("order_ref_max_measured", measured)
    get_logger("tests.ibkr_live").info("ibkr.order_ref_measured", measured=measured, kept=results)
    assert measured >= broker.order_settings.order_ref_max_length, results


def test_live_account_reports_its_margin_type(client, broker, record_property):
    """Record which account value names the margin type (roadmap 19.13). A
    margin profile is refused unless it reads ``margin``. The tags are
    logged and recorded, never asserted, since a cash paper account is
    fine too."""
    from stonks.execution.brokers.ibkr.broker import MARGIN_TYPE_TAGS, reported_account_type

    values = client.account_values(ACCOUNT)
    tags = {v.tag: v.value for v in values if v.tag in MARGIN_TYPE_TAGS}
    reported = reported_account_type(values, account_id=ACCOUNT)
    record_property("margin_type_tags", tags)
    record_property("reported_account_type", reported)
    get_logger("tests.ibkr_live").info("ibkr.margin_type", tags=tags, reported=reported)
    assert broker.fetch_account().reported_type == reported


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


def test_live_executions_and_completed_orders(client, broker):
    since = datetime.now(UTC) - timedelta(days=1)
    for execution in broker.executions(since):
        assert execution.broker_exec_id and execution.quantity > 0 and execution.price > 0
        assert execution.executed_at >= since
    for trade in client.completed_trades():
        assert trade.status


def test_live_reconnect_rechecks_the_account(client, broker):
    """A dropped session comes back on the next call, and the account check
    runs again before anything is sent."""
    broker.login_check()
    before = client.status().connects
    client.disconnect()
    assert not client.status().connected
    broker.ensure_ready()  # reconnects
    status = client.status()
    assert status.connected and status.connects == before + 1
    assert broker.account_id == ACCOUNT
    assert broker.fetch_account().equity > 0


def test_live_quotes(broker):
    quotes = broker.quotes(["AAPL.US"])
    if quotes:
        assert quotes["AAPL.US"].reference is not None or quotes["AAPL.US"].mid is None


def test_live_london_prices_are_in_pounds(client, broker):
    """Roadmap 19.16: IBKR quotes London in pence (price magnifier 100) and
    the adapter hands out pounds."""
    resolved = ContractResolver(client).resolve("VOD.LSE")
    assert resolved.contract.currency == "GBP"
    assert resolved.price_magnifier == 100
    quote = broker.quotes(["VOD.LSE"]).get("VOD.LSE")
    if quote is not None and quote.reference is not None:
        assert 0.05 < quote.reference < 20.0  # Vodafone in pounds, never in pence
