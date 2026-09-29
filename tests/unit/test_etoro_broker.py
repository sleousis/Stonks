"""The eToro ``Broker``: market opens in units, sells as closes of the
oldest positions, refusals, order states by client id, cancels and the
account reads (hermetic: a fake eToro)."""

from __future__ import annotations

import pytest

from stonks.core.types import Order
from stonks.execution.brokers.base import (
    AccountReader,
    BrokerUnavailableError,
    OpenOrderSource,
    OrderCanceller,
    OrderOutcomeUnknownError,
    OrderRejectedError,
    OrderStateSource,
)
from stonks.execution.brokers.etoro.account import EtoroAccount
from stonks.execution.brokers.etoro.broker import EtoroBroker, request_id
from stonks.execution.brokers.etoro.client import EtoroClient
from stonks.execution.brokers.etoro.instruments import InstrumentCatalog
from stonks.execution.brokers.etoro.settings import EtoroConfig
from tests.fakes.etoro_server import API_KEY, USER_KEY, FakeEtoro


def broker(fake: FakeEtoro, **config) -> EtoroBroker:
    client = EtoroClient(EtoroConfig(**config), api_key=API_KEY, user_key=USER_KEY,
                         transport=fake.transport(), sleep=lambda s: None)  # fmt: skip
    return EtoroBroker(
        EtoroAccount(client, fake.env), InstrumentCatalog(), account_id=f"{fake.env}-{fake.cid}"
    )


def buy(ticker: str = "AAPL.US", qty: float = 2.0, cid: str = "c-buy", **kw) -> Order:
    return Order(client_id=cid, ticker=ticker, side="buy", quantity=qty, **kw)


def sell(ticker: str = "AAPL.US", qty: float = 2.0, cid: str = "c-sell", **kw) -> Order:
    return Order(client_id=cid, ticker=ticker, side="sell", quantity=qty, **kw)


def test_capabilities_and_real_money_flag():
    demo = broker(FakeEtoro(env="demo"))
    real = broker(FakeEtoro(env="real"))
    for proto in (OrderStateSource, OrderCanceller, AccountReader, OpenOrderSource):
        assert isinstance(demo, proto)
    assert demo.real_money is False
    assert real.real_money is True


def test_fetch_portfolio_adds_up_plain_positions_by_ticker():
    fake = FakeEtoro()
    fake.add_position(1001, 3, 150.0)
    fake.add_position(1001, 2, 160.0)
    fake.add_position(1, 1000, 1.1, settlement=0)  # EURUSD CFD: not covered
    p = broker(fake).fetch_portfolio()
    assert p.positions == {"AAPL.US": 5.0}
    assert p.cash == pytest.approx(10_000.0)


def test_a_buy_opens_a_real_unleveraged_position_in_units_and_fills():
    fake = FakeEtoro()
    b = broker(fake)
    assert b.place_order(buy(qty=2.5)) is None
    (order,) = fake.orders.values()
    assert order["referenceId"] == request_id("c-buy")
    assert order["settlementType"] == "real" and order["requestedUnits"] == 2.5
    state = b.get_order_state("c-buy")
    assert state is not None
    assert (state.state, state.status, state.filled_quantity) == ("filled", "filled", 2.5)
    assert state.avg_fill_price == pytest.approx(200.0)
    assert state.broker_order_id == str(order["orderId"])
    assert b.fetch_portfolio().positions == {"AAPL.US": 2.5}


def test_a_repeated_submit_sends_nothing_new():
    fake = FakeEtoro()
    b = broker(fake)
    b.place_order(buy())
    b.place_order(buy())
    assert len(fake.orders) == 1
    # a fresh process finds the order by its client id and does not resend
    other = broker(fake)
    other.place_order(buy())
    assert len(fake.orders) == 1


def test_a_fresh_process_reads_the_order_by_client_id():
    fake = FakeEtoro(fill_mode="wait")
    broker(fake).place_order(buy())
    later = broker(fake)
    state = later.get_order_state("c-buy")
    assert state is not None and state.state == "accepted" and state.status == "pending"
    assert later.get_order_state("never-sent") is None


def test_partial_and_rejected_opens():
    fake = FakeEtoro(fill_mode="partial")
    b = broker(fake)
    b.place_order(buy(qty=4))
    state = b.get_order_state("c-buy")
    assert state is not None
    assert (state.state, state.filled_quantity, state.quantity) == ("cancelled", 2.0, 4.0)

    fake.fill_mode = "reject"
    b.place_order(buy(cid="c-2"))
    state = b.get_order_state("c-2")
    assert state is not None and state.state == "rejected" and state.filled_quantity == 0


def test_cancel_a_waiting_open():
    fake = FakeEtoro(fill_mode="wait")
    b = broker(fake)
    b.place_order(buy())
    assert b.cancel_order("c-buy") is True
    state = b.get_order_state("c-buy")
    assert state is not None and state.state == "cancelled"
    assert b.cancel_order("c-buy") is False  # nothing left to cancel
    assert b.cancel_order("unknown") is False


@pytest.mark.parametrize(
    ("order", "words"),
    [
        (buy(order_type="limit", limit_price=190.0), "market orders only"),
        (buy(ticker="CFDX.US"), "only as a CFD"),
        (buy(ticker="WHOLE.US", qty=1.5), "whole units"),
        (buy(ticker="EURUSD.FOREX"), "cannot be traded"),
        (buy(ticker="ZZZZ.US"), "lists no instrument"),
        (sell(position_effect="open"), "short"),
        (buy(position_effect="close"), "short"),
        (buy(outside_rth=True), "outside regular hours"),
    ],
)
def test_refusals_send_nothing(order, words):
    fake = FakeEtoro()
    fake.add_position(1001, 5, 150.0)
    with pytest.raises(OrderRejectedError) as info:
        broker(fake).place_order(order)
    assert words in str(info.value)
    assert not fake.orders and not fake.close_orders


def test_amount_only_instruments_are_bought_by_usd_amount_at_the_ask():
    fake = FakeEtoro()
    b = broker(fake)
    b.place_order(buy(ticker="AMTO.US", qty=3))
    (order,) = fake.orders.values()
    assert order["requestType"] == "byAmount"
    assert order["requestedAmount"] == pytest.approx(150.0)  # 3 * ask 50


def test_a_sell_closes_the_oldest_positions_first_the_last_in_part():
    fake = FakeEtoro()
    old = fake.add_position(1001, 2, 150.0, opened="2026-01-02T15:00:00Z")
    new = fake.add_position(1001, 5, 170.0, opened="2026-02-02T15:00:00Z")
    copied = fake.add_position(1001, 9, 140.0, opened="2025-01-02T15:00:00Z")
    fake.positions[-1]["mirrorID"] = 77  # a copied trader's position is never touched
    b = broker(fake)
    b.place_order(sell(qty=3))
    closes = sorted(fake.close_orders.values(), key=lambda o: o["orderID"])
    assert [(c["positionID"], c["units"]) for c in closes] == [(old, 2), (new, 1)]
    state = b.get_order_state("c-sell")
    assert state is not None
    assert (state.state, state.filled_quantity) == ("filled", 3.0)
    assert state.broker_order_id == "close:" + ",".join(str(c["orderID"]) for c in closes)
    assert state.avg_fill_price == pytest.approx(199.5)
    left = {p["positionID"]: p["units"] for p in fake.positions}
    assert left == {new: 4, copied: 9}


def test_selling_more_than_held_is_refused():
    fake = FakeEtoro()
    fake.add_position(1001, 2, 150.0)
    fake.add_position(1001, 5, 150.0, leverage=2)  # leveraged: not ours to close
    with pytest.raises(OrderRejectedError, match="holds 2"):
        broker(fake).place_order(sell(qty=3))
    assert not fake.close_orders


def test_close_states_come_back_from_the_ledger_in_a_fresh_process():
    fake = FakeEtoro(fill_mode="wait")
    fake.add_position(1001, 4, 150.0)
    b = broker(fake)
    b.place_order(sell(qty=4))
    state = b.get_order_state("c-sell")
    assert state is not None and state.state == "accepted"

    class Ledger:
        def sql(self, query, params=()):
            return [{"ticker": "AAPL.US", "side": "sell", "quantity": 4.0,
                     "broker_order_id": state.broker_order_id, "client_id": "c-sell"}]  # fmt: skip

    (order_id,) = fake.close_orders
    fake.release(order_id)
    later = EtoroBroker(b._account, InstrumentCatalog(), account_id="x", state=Ledger())
    again = later.get_order_state("c-sell")
    assert again is not None and again.state == "filled" and again.filled_quantity == 4


def test_cancel_a_waiting_close():
    fake = FakeEtoro(fill_mode="wait")
    fake.add_position(1001, 4, 150.0)
    b = broker(fake)
    b.place_order(sell(qty=4))
    assert b.cancel_order("c-sell") is True
    state = b.get_order_state("c-sell")
    assert state is not None and state.state == "cancelled"


def test_a_rejected_close_is_rejected():
    fake = FakeEtoro(fill_mode="reject")
    fake.add_position(1001, 4, 150.0)
    b = broker(fake)
    b.place_order(sell(qty=4))
    state = b.get_order_state("c-sell")
    assert state is not None and state.state == "rejected"


def test_an_open_with_no_answer_is_outcome_unknown_and_found_later():
    fake = FakeEtoro()
    fake.queue("/api/v2/trading/execution/demo/orders", 504)
    b = broker(fake)
    with pytest.raises(OrderOutcomeUnknownError):
        b.place_order(buy())
    assert b.get_order_state("c-buy") is None  # never reached the fake


def test_an_outage_is_a_broker_outage():
    fake = FakeEtoro()
    for _ in range(4):
        fake.queue("/api/v1/trading/info/demo/pnl", 503)
    with pytest.raises(BrokerUnavailableError):
        broker(fake).fetch_portfolio()


def test_rate_limits_back_off_then_go_through():
    fake = FakeEtoro()
    fake.queue("/api/v2/trading/execution/demo/orders", 429, {"Retry-After": "1"})
    b = broker(fake)
    b.place_order(buy())
    assert len(fake.orders) == 1


def test_account_reads():
    fake = FakeEtoro(credit=5_000.0)
    fake.add_position(1001, 10, 150.0)
    fake.positions[-1]["unrealizedPnL"]["pnL"] = 250.0
    account = broker(fake).fetch_account()
    assert account.cash == pytest.approx(5_000.0)
    assert account.equity == pytest.approx(5_000.0 + 1_500.0 + 250.0)
    assert account.currency == "USD" and account.account_type == "cash"


def test_open_orders_name_our_client_ids():
    fake = FakeEtoro(fill_mode="wait")
    fake.add_position(1002, 3, 300.0)
    b = broker(fake)
    b.place_order(buy())
    b.place_order(sell(ticker="MSFT.US", qty=3))
    fake.orders[fake._next_id + 100] = {  # a hand-placed open order
        "orderId": fake._next_id + 100, "referenceId": "x", "instrumentId": 1002,
        "symbol": "MSFT", "settlementType": "real", "requestedUnits": 1.0,
        "requestedAmount": None, "status": 11, "errorCode": 0, "errorMessage": "",
        "executions": [], "requestTime": "", "requestType": "byUnits",
    }  # fmt: skip
    working = {(o.ticker, o.side, o.client_id) for o in b.open_orders()}
    assert working == {
        ("AAPL.US", "buy", "c-buy"),
        ("MSFT.US", "sell", "c-sell"),
        ("MSFT.US", "buy", None),
    }
