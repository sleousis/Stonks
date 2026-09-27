"""``IbAsyncClient`` over a stand-in ``ib_async.IB`` (roadmap 19.2): the
session thread, reconnects, link state and the vendor-object mapping. The
objects are real ``ib_async`` dataclasses. No socket is opened."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from ib_async import (
    AccountValue,
    CommissionReport,
    Contract,
    ContractDetails,
    Execution,
    Fill,
    Order,
    OrderState,
    OrderStatus,
    Position,
    TagValue,
    Ticker,
    Trade,
    TradeLogEntry,
)

from stonks.execution.brokers.ibkr.client import (
    IbApiError,
    IbConnectionError,
    IbContract,
    IbContractQuery,
    IbEndpoint,
    IbOrderRequest,
)
from stonks.execution.brokers.ibkr.ib_async_client import (
    UNSET,
    IbAsyncClient,
    from_trade,
    query_contract,
    to_order,
)

NOW = datetime(2026, 9, 28, 13, 30, tzinfo=UTC)
AAPL = Contract(conId=265598, symbol="AAPL", secType="STK", exchange="SMART",
                primaryExchange="NASDAQ", currency="USD", tradingClass="NMS")  # fmt: skip
IB_AAPL = IbContract(265598, "AAPL", "STK", "USD", "SMART", "NASDAQ")


class Event:
    def __init__(self) -> None:
        self.handlers: list = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def emit(self, *args):
        for h in self.handlers:
            h(*args)


class FakeIB:
    """The slice of ``ib_async.IB`` the client uses."""

    def __init__(self) -> None:
        self.errorEvent = Event()
        self.connected = False
        self.refuse = 0
        self.connect_calls = 0
        self.market_data_type = None
        self.trades: list[Trade] = []
        self.completed: list[Trade] = []
        self.fill_list: list[Fill] = []
        self.values: list[AccountValue] = []
        self.summary: list[AccountValue] = []
        self.position_list: list[Position] = []
        self.ack_status = "Submitted"
        self.order_error: tuple[int, str] | None = None
        self.cancelled: list[int] = []
        self.global_cancels = 0
        self.details: list[ContractDetails] = []
        self.queries: list[Contract] = []

    def isConnected(self):
        return self.connected

    async def connectAsync(self, host, port, clientId, timeout, readonly):  # noqa: ASYNC109
        self.connect_calls += 1
        if self.refuse:
            self.refuse -= 1
            raise ConnectionRefusedError("refused")
        self.connected = True

    def disconnect(self):
        self.connected = False

    def reqMarketDataType(self, kind):
        self.market_data_type = kind

    def managedAccounts(self):
        return ["DU1"]

    async def reqCurrentTimeAsync(self):
        return NOW.replace(tzinfo=None)

    async def reqContractDetailsAsync(self, contract):
        self.queries.append(contract)
        return self.details

    def openTrades(self):
        return [t for t in self.trades if t.orderStatus.status not in ("Filled", "Cancelled")]

    async def reqCompletedOrdersAsync(self, api_only):
        return self.completed

    async def reqExecutionsAsync(self):
        return self.fill_list

    def fills(self):
        return self.fill_list

    def positions(self, account):
        return self.position_list

    def accountValues(self, account):
        return self.values

    async def accountSummaryAsync(self, account):
        return self.summary

    async def whatIfOrderAsync(self, contract, order):
        return OrderState(initMarginChange="100.5", maintMarginChange="", equityWithLoanAfter="9",
                          commission=UNSET, commissionCurrency="", warningText="")  # fmt: skip

    async def reqTickersAsync(self, *contracts):
        out = []
        for c in contracts:
            t = Ticker(contract=c, time=NOW, marketDataType=3)
            t.bid, t.ask = 1.0, 1.2
            out.append(t)
        return out

    def placeOrder(self, contract, order):
        order.orderId = len(self.trades) + 1
        trade = Trade(contract, order, OrderStatus(orderId=order.orderId, status="PendingSubmit",
                                                   permId=500 + order.orderId), [], [])  # fmt: skip
        self.trades.append(trade)
        loop = asyncio.get_running_loop()
        if self.order_error is not None:
            code, msg = self.order_error
            loop.call_later(0.01, lambda: self.errorEvent.emit(order.orderId, code, msg, contract))
        elif self.ack_status:
            status = self.ack_status
            loop.call_later(0.01, lambda: setattr(trade.orderStatus, "status", status))
        return trade

    def cancelOrder(self, order):
        self.cancelled.append(order.orderId)

    def reqGlobalCancel(self):
        self.global_cancels += 1


def client(ib: FakeIB | None = None, **kw) -> tuple[IbAsyncClient, FakeIB]:
    ib = ib or FakeIB()
    ep = IbEndpoint("gw", 4004, 11, connect_timeout=1, request_timeout=1, reconnect_deadline=5)
    c = IbAsyncClient(ep, ib_factory=lambda: ib, sleep=lambda s: None, **kw)
    return c, ib


@pytest.fixture
def pair():
    c, ib = client()
    yield c, ib
    c.close()


def test_connect_retries_then_sets_market_data_type(pair):
    c, ib = pair
    ib.refuse = 2
    c.connect()
    assert ib.connect_calls == 3
    assert ib.market_data_type == 3
    s = c.status()
    assert s.connected and s.link_ok and s.connects == 1
    c.connect()  # already connected: no-op
    assert ib.connect_calls == 3


def test_connect_gives_up_at_the_deadline():
    ib = FakeIB()
    ib.refuse = 10_000
    ep = IbEndpoint("gw", 4004, 11, connect_timeout=1, request_timeout=1, reconnect_deadline=3)
    c = IbAsyncClient(ep, ib_factory=lambda: ib, sleep=lambda s: None)
    try:
        with pytest.raises(IbConnectionError):
            c.connect()
    finally:
        c.close()


def test_calls_need_a_connection(pair):
    c, _ = pair
    with pytest.raises(IbConnectionError):
        c.managed_accounts()


def test_error_event_tracks_the_link(pair):
    c, ib = pair
    c.connect()
    ib.errorEvent.emit(-1, 2104, "farm ok", None)
    assert c.status().last_error is None
    ib.errorEvent.emit(-1, 1100, "lost", None)
    assert not c.status().link_ok
    ib.errorEvent.emit(-1, 1102, "restored", None)
    assert c.status().link_ok
    ib.errorEvent.emit(-1, 10197, "competing", None)
    s = c.status()
    assert s.competing and s.last_error == (10197, "competing")
    ib.connected = False
    c.connect()
    assert not c.status().competing


def test_reads_map_to_our_types(pair):
    c, ib = pair
    c.connect()
    assert c.managed_accounts() == ["DU1"]
    assert c.server_time() == NOW
    ib.details = [ContractDetails(contract=AAPL, minTick=0.01, priceMagnifier=1, longName="Apple",
                                  secIdList=[TagValue("ISIN", "US0378331005")])]  # fmt: skip
    (d,) = c.contract_details(IbContractQuery("AAPL", "USD", isin="US0378331005"))
    assert d.contract == IbContract(265598, "AAPL", "STK", "USD", "SMART", "NASDAQ", "NMS")
    assert (d.min_tick, d.isin, d.long_name) == (0.01, "US0378331005", "Apple")
    assert ib.queries[0].secIdType == "ISIN"

    ib.position_list = [Position("DU1", AAPL, 10.0, 150.0)]
    (p,) = c.positions("DU1")
    assert (p.account, p.position, p.contract.con_id) == ("DU1", 10.0, 265598)

    ib.summary = [AccountValue("DU1", "NetLiquidation", "1000", "USD", "")]
    assert c.account_values("DU1")[0].tag == "NetLiquidation"
    ib.values = [AccountValue("DU1", "CashBalance", "5", "EUR", "")]
    assert c.account_values("DU1")[0].currency == "EUR"

    w = c.what_if(IB_AAPL, _req())
    assert (w.init_margin_change, w.maint_margin_change, w.commission) == (100.5, None, None)
    assert w.equity_with_loan_after == 9.0

    (s,) = c.snapshots([IB_AAPL])
    assert (s.con_id, s.bid, s.ask, s.last, s.market_data_type) == (265598, 1.0, 1.2, None, 3)


def test_executions_carry_late_commissions(pair):
    c, ib = pair
    c.connect()
    exe = Execution(execId="e1", time=NOW, acctNumber="DU1", side="BOT", shares=10, price=201.0,
                    permId=77, orderRef="ref-1")  # fmt: skip
    ib.fill_list = [Fill(AAPL, exe, CommissionReport(), NOW)]
    (e,) = c.executions()
    assert (e.exec_id, e.order_ref, e.side, e.shares, e.commission) == ("e1", "ref-1", "BOT", 10,
                                                                         None)  # fmt: skip
    ib.fill_list = [Fill(AAPL, exe, CommissionReport(execId="e1", commission=1.1,
                                                     currency="USD"), NOW)]  # fmt: skip
    (e,) = c.executions()
    assert (e.commission, e.commission_currency) == (1.1, "USD")


def _req(**kw) -> IbOrderRequest:
    base = {"action": "BUY", "total_quantity": 10.0, "order_type": "LMT", "tif": "OPG",
            "order_ref": "ref-1", "account": "DU1", "limit_price": 202.0}  # fmt: skip
    base.update(kw)
    return IbOrderRequest(**base)


def test_to_order_and_query_contract():
    o = to_order(_req(aux_price=None))
    assert (o.action, o.totalQuantity, o.orderType, o.tif, o.orderRef, o.account, o.lmtPrice) == (
        "BUY",
        10.0,
        "LMT",
        "OPG",
        "ref-1",
        "DU1",
        202.0,
    )
    assert o.auxPrice == UNSET and o.outsideRth is False and o.transmit is True
    stp = to_order(_req(order_type="STP", limit_price=None, aux_price=90.0))
    assert stp.auxPrice == 90.0 and stp.lmtPrice == UNSET
    q = query_contract(IbContractQuery("BRK B", "USD", primary_exchange="NYSE"))
    assert (q.symbol, q.primaryExchange, q.exchange, q.secIdType) == ("BRK B", "NYSE", "SMART", "")


def test_place_order_waits_for_the_acknowledgement(pair):
    c, ib = pair
    c.connect()
    t = c.place_order(IB_AAPL, _req())
    assert (t.order_id, t.perm_id, t.status, t.order_ref) == (1, 501, "Submitted", "ref-1")


def test_place_order_raises_the_order_error(pair):
    c, ib = pair
    c.connect()
    ib.order_error = (201, "Order rejected - reason: no funds")
    with pytest.raises(IbApiError) as info:
        c.place_order(IB_AAPL, _req())
    assert info.value.code == 201


def test_place_order_times_out_without_an_answer(pair):
    c, ib = pair
    c.connect()
    ib.ack_status = ""
    with pytest.raises(TimeoutError):
        c.place_order(IB_AAPL, _req())


def test_cancel_and_global_cancel(pair):
    c, ib = pair
    c.connect()
    c.place_order(IB_AAPL, _req())
    assert len(c.open_trades()) == 1
    c.cancel_order(1)
    assert ib.cancelled == [1]
    with pytest.raises(IbApiError) as info:
        c.cancel_order(99)
    assert info.value.code == 135
    c.global_cancel()
    assert ib.global_cancels == 1


def test_completed_trades_use_the_filled_quantity_and_log_reason(pair):
    c, ib = pair
    c.connect()
    order = Order(orderId=3, permId=9, action="SELL", totalQuantity=5, orderRef="r", tif="DAY",
                  account="DU1", filledQuantity=5)  # fmt: skip
    trade = Trade(AAPL, order, OrderStatus(orderId=3, status="Filled"), [],
                  [TradeLogEntry(NOW, "Inactive", "no funds", 201)])  # fmt: skip
    ib.completed = [trade]
    (t,) = c.completed_trades()
    assert (t.action, t.filled, t.perm_id, t.status, t.reason, t.avg_fill_price) == (
        "SELL",
        5.0,
        9,
        "Filled",
        "no funds",
        None,
    )


def test_from_trade_without_fills():
    order = Order(orderId=1, action="BUY", totalQuantity=2, orderRef="")
    t = from_trade(Trade(AAPL, order, OrderStatus(orderId=1, status="Submitted", permId=4), [], []))
    assert (t.filled, t.reason, t.tif, t.order_ref, t.perm_id) == (0.0, None, None, "", 4)


def test_to_order_carries_the_oca_group():
    o = to_order(_req(oca_group="stk-oca-1", oca_type=2))
    assert (o.ocaGroup, o.ocaType) == ("stk-oca-1", 2)
    plain = to_order(_req())
    assert plain.ocaGroup == ""
