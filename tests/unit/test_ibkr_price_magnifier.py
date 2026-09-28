"""Pence-quoted markets: prices through the contract's price magnifier
(roadmap 19.16). IBKR quotes a London stock in pence (magnifier 100).
Stonks works in pounds everywhere, so the adapter divides every price it
reads and multiplies every price it sends."""

from __future__ import annotations

from datetime import timedelta

import pytest

from stonks.core.instruments import InstrumentSpec
from stonks.core.types import Order
from stonks.execution.brokers.ibkr.broker import IbkrBroker
from stonks.execution.brokers.ibkr.client import IbSnapshot
from stonks.execution.brokers.ibkr.contracts import (
    ContractResolver,
    to_ib_price,
    to_major,
)
from stonks.execution.brokers.ibkr.orders import to_ib_order
from stonks.execution.brokers.ibkr.settings import IbkrOrderSettings
from tests.fakes.ib_gateway import AAPL, T0, FakeIbGateway, stock

#: A London stock in pence: IBKR's minimum tick is 0.05 pence.
LLOY = stock(7777, "LLOY", currency="GBP", primary="LSE", min_tick=0.05, magnifier=100)
ACCOUNT = "DU1234567"


def sell_gbx(client_id: str = "t1-s1-LLOY.LSE-sell", **kw) -> Order:
    base = {"client_id": client_id, "ticker": "LLOY.LSE", "side": "sell", "quantity": 100.0,
            "position_effect": "close"}  # fmt: skip
    base.update(kw)
    return Order(**base)


def make() -> tuple[IbkrBroker, FakeIbGateway]:
    gw = FakeIbGateway(contracts=(AAPL, LLOY))
    return IbkrBroker(gw, mode="paper"), gw


def test_price_conversions_are_exact():
    assert to_major(123.45, 100) == 1.2345
    assert to_ib_price(1.2345, 100) == 123.45
    assert to_major(200.0, 1) == 200.0 and to_ib_price(200.0, 1) == 200.0


def test_spec_tick_size_is_in_pounds():
    gw = FakeIbGateway(contracts=(LLOY,))
    gw.connect()
    spec = ContractResolver(gw).spec("LLOY.LSE")
    assert spec.currency == "GBP"
    assert spec.tick_size == pytest.approx(0.0005)


def _spec() -> InstrumentSpec:
    return InstrumentSpec.spot("LLOY.LSE", currency="GBP", tick_size=0.0005)


def test_limit_price_goes_out_in_pence_on_the_pence_grid():
    order = Order("c1", "LLOY.LSE", "buy", 100, order_type="limit", limit_price=1.23456)
    req = to_ib_order(
        order, _spec(), account=ACCOUNT, settings=IbkrOrderSettings(), price_magnifier=100
    )
    # 123.456 pence, a buy rounds down to the 0.05 pence grid
    assert req.limit_price == pytest.approx(123.45)


def test_collar_and_stop_prices_go_out_in_pence():
    settings = IbkrOrderSettings(collar_bps=100)
    collared = Order("c2", "LLOY.LSE", "buy", 100, decision_price=1.0)
    req = to_ib_order(collared, _spec(), account=ACCOUNT, settings=settings, price_magnifier=100)
    assert (req.order_type, req.limit_price) == ("LMT", pytest.approx(101.0))
    stop = Order("c3", "LLOY.LSE", "sell", 100, order_type="stop", stop_price=0.9,
                 position_effect="close")  # fmt: skip
    req = to_ib_order(stop, _spec(), account=ACCOUNT, settings=settings, price_magnifier=100)
    assert req.aux_price == pytest.approx(90.0)


def test_broker_sends_pence_and_reads_pounds():
    broker, gw = make()
    broker.place_order(sell_gbx(order_type="limit", limit_price=1.5))
    _, request = gw.sent[-1]
    assert request.limit_price == pytest.approx(150.0)
    gw.fill("t1-s1-LLOY.LSE-sell", 100, 150.25, commission=3.0, currency="GBP")
    state = broker.get_order_state("t1-s1-LLOY.LSE-sell")
    assert state is not None and state.avg_fill_price == pytest.approx(1.5025)
    [execution] = broker.executions(T0 - timedelta(hours=1))
    assert execution.price == pytest.approx(1.5025)
    assert execution.commission == 3.0  # money, not a price: never scaled
    [fill] = broker.reconcile()
    assert fill.price == pytest.approx(1.5025)


def test_us_prices_are_untouched():
    broker, gw = make()
    broker.place_order(
        Order("t1-s1-AAPL.US-buy", "AAPL.US", "buy", 10, order_type="limit", limit_price=200.0)
    )
    assert gw.sent[-1][1].limit_price == 200.0
    gw.fill("t1-s1-AAPL.US-buy", 10, 199.5)
    assert broker.reconcile()[0].price == 199.5


def test_quotes_come_back_in_pounds():
    broker, gw = make()
    gw.snapshot_data[LLOY.contract.con_id] = IbSnapshot(
        con_id=LLOY.contract.con_id, last=150.0, bid=149.95, ask=150.05, time=T0
    )
    quote = broker.quotes(["LLOY.LSE"])["LLOY.LSE"]
    assert quote.last == pytest.approx(1.5)
    assert quote.bid == pytest.approx(1.4995)
    assert quote.ask == pytest.approx(1.5005)


def test_what_if_sends_the_order_in_pence():
    broker, gw = make()
    broker.what_if(sell_gbx(order_type="limit", limit_price=2.0))
    _, request = gw.what_ifs[-1]
    assert request.limit_price == pytest.approx(200.0)


def test_executions_of_an_unresolved_contract_read_its_magnifier():
    # a fresh broker (cold cache) still converts a London execution: it
    # resolves the contract the execution names
    broker, gw = make()
    broker.place_order(sell_gbx(order_type="limit", limit_price=1.5))
    gw.fill("t1-s1-LLOY.LSE-sell", 100, 150.0)
    cold = IbkrBroker(gw, mode="paper")
    assert cold.executions(T0 - timedelta(hours=1))[0].price == pytest.approx(1.5)
