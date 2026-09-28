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
        self.shortable: dict[int, tuple[float, float]] = {}
        self.mkt_requests: list = []
        self.mkt_cancels: list = []
        self.all_open: list[Trade] = []
        self.all_open_calls = 0

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

    def reqMktData(self, contract, generic, snapshot, regulatory):
        self.mkt_requests.append((contract.conId, generic, snapshot))
        t = Ticker(contract=contract, time=NOW)
        answer = self.shortable.get(contract.conId)
        if answer is not None:
            loop = asyncio.get_running_loop()

            def arrive():
                t.shortable, t.shortableShares = answer

            loop.call_later(0.01, arrive)
        return t

    def cancelMktData(self, contract):
        self.mkt_cancels.append(contract.conId)
        return True

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

    async def reqAllOpenOrdersAsync(self):
        self.all_open_calls += 1
        return self.all_open


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


class Wrapper:
    """The slice of ``ib_async.Wrapper`` the decoder calls for a completed order."""

    def __init__(self, ib: FakeIB) -> None:
        self.ib = ib

    def completedOrder(self, contract, order, order_state):
        self.ib.completed.append(
            Trade(contract, order, OrderStatus(orderId=order.orderId, status=order_state.status))
        )


def test_completed_trades_carry_the_cancel_origin():
    # roadmap 19.16: ib_async drops the completed status, so the client
    # keeps it by permId as the decoder hands it over
    ib = FakeIB()
    ib.wrapper = Wrapper(ib)  # type: ignore[attr-defined]
    c, _ = client(ib)
    try:
        c.connect()
        by_hand = Order(orderId=4, permId=11, action="BUY", totalQuantity=5, orderRef="a",
                        tif="OPG")  # fmt: skip
        expired = Order(orderId=5, permId=12, action="BUY", totalQuantity=5, orderRef="b",
                        tif="OPG")  # fmt: skip
        ib.wrapper.completedOrder(  # type: ignore[attr-defined]
            AAPL, by_hand, OrderState(status="Cancelled", completedStatus="Cancelled by Trader")
        )
        ib.wrapper.completedOrder(  # type: ignore[attr-defined]
            AAPL, expired, OrderState(status="Cancelled", completedStatus="Cancelled by System")
        )
        origins = {t.order_ref: t.cancel_origin for t in c.completed_trades()}
        assert origins == {"a": "trader", "b": "system"}
    finally:
        c.close()


def test_from_trade_without_fills():
    order = Order(orderId=1, action="BUY", totalQuantity=2, orderRef="")
    t = from_trade(Trade(AAPL, order, OrderStatus(orderId=1, status="Submitted", permId=4), [], []))
    assert (t.filled, t.reason, t.tif, t.order_ref, t.perm_id) == (0.0, None, None, "", 4)


def test_shortability_streams_generic_tick_236_then_cancels(pair):
    c, ib = pair
    c.connect()
    ib.shortable = {265598: (3.0, 12_000.0)}
    other = IbContract(1, "NOPE", "STK", "USD", "SMART", "NYSE")
    first, second = c.shortability([IB_AAPL, other])
    assert (first.con_id, first.indicator, first.shares) == (265598, 3.0, 12_000.0)
    assert (second.con_id, second.indicator, second.shares) == (1, None, None)
    assert [(cid, g, snap) for cid, g, snap in ib.mkt_requests] == [
        (265598, "236", False),
        (1, "236", False),
    ]
    assert ib.mkt_cancels == [265598, 1]


def test_to_order_carries_the_oca_group():
    o = to_order(_req(oca_group="stk-oca-1", oca_type=2))
    assert (o.ocaGroup, o.ocaType) == ("stk-oca-1", 2)
    plain = to_order(_req())
    assert plain.ocaGroup == ""


def test_to_order_carries_the_algo():
    """Roadmap 23.16: IBKR's algoStrategy and algoParams as TagValues."""
    o = to_order(_req(algo_strategy="Vwap", algo_params=(("maxPctVol", "0.1"),)))
    assert o.algoStrategy == "Vwap"
    assert [(t.tag, t.value) for t in o.algoParams] == [("maxPctVol", "0.1")]
    assert to_order(_req()).algoStrategy == ""


def test_all_open_trades_reads_every_clients_orders(pair):
    """Roadmap 19.17: ``reqAllOpenOrders`` with the placing client id."""
    c, ib = pair
    c.connect()
    order = Order(orderId=4, clientId=11, permId=77, action="BUY", totalQuantity=3,
                  orderRef="tick-ref", tif="DAY", account="DU1")  # fmt: skip
    ib.all_open = [Trade(AAPL, order, OrderStatus(orderId=4, status="Submitted"), [], [])]
    (t,) = c.all_open_trades()
    assert (t.order_ref, t.client_id, t.perm_id, t.order_id) == ("tick-ref", 11, 77, 4)
    assert ib.all_open_calls == 1


def test_cancel_prefers_this_clients_order_on_an_id_clash(pair):
    """Order ids are per client: a master sees two orders with id 1."""
    c, ib = pair
    c.connect()
    theirs = Order(orderId=1, clientId=16, action="BUY", totalQuantity=1, orderRef="a")
    mine = Order(orderId=1, clientId=11, action="BUY", totalQuantity=1, orderRef="b")
    for o in (theirs, mine):
        ib.trades.append(Trade(AAPL, o, OrderStatus(orderId=1, status="Submitted"), [], []))
    cancelled: list[str] = []
    ib.cancelOrder = lambda order: cancelled.append(order.orderRef)
    c.cancel_order(1)
    assert cancelled == ["b"]


# ---- options (roadmap 17.8) -------------------------------------------------------------


def test_option_contracts_keep_their_expiry_right_strike_and_multiplier():
    from stonks.execution.brokers.ibkr.ib_async_client import from_contract

    opt = Contract(conId=1001, symbol="AAPL", secType="OPT", exchange="SMART", currency="USD",
                   lastTradeDateOrContractMonth="20261016", strike=200.0, right="C",
                   multiplier="100")  # fmt: skip
    got = from_contract(opt)
    assert (got.sec_type, got.last_trade_date, got.strike, got.right, got.multiplier) == (
        "OPT",
        "20261016",
        200.0,
        "C",
        "100",
    )
    assert from_contract(AAPL).strike is None


def test_an_option_query_names_every_field():
    q = query_contract(
        IbContractQuery(
            symbol="AAPL",
            currency="USD",
            sec_type="OPT",
            last_trade_date="20261016",
            strike=200.0,
            right="P",
            multiplier="100",
        )
    )
    assert (q.secType, q.lastTradeDateOrContractMonth, q.strike, q.right, q.multiplier) == (
        "OPT",
        "20261016",
        200.0,
        "P",
        "100",
    )


def test_a_bag_contract_carries_its_legs():
    from stonks.execution.brokers.ibkr.client import IbComboLeg
    from stonks.execution.brokers.ibkr.ib_async_client import from_contract, to_contract

    bag = IbContract(0, "AAPL", "BAG", "USD",
                     combo_legs=(IbComboLeg(1001, 1, "BUY"), IbComboLeg(1002, 1, "SELL")))  # fmt: skip
    c = to_contract(bag)
    assert c.secType == "BAG" and [(leg.conId, leg.action) for leg in c.comboLegs] == [
        (1001, "BUY"),
        (1002, "SELL"),
    ]
    assert from_contract(c).combo_legs == bag.combo_legs


def test_option_tickers_map_ibkrs_model_greeks():
    from ib_async import OptionComputation

    from stonks.execution.brokers.ibkr.ib_async_client import from_option_ticker

    t = Ticker(contract=Contract(conId=1001, right="C"), time=NOW, marketDataType=1)
    t.bid, t.ask, t.callOpenInterest = 5.0, 5.2, 900.0
    t.modelGreeks = OptionComputation(0, 0.31, 0.55, 5.1, 0.0, 0.02, 0.25, -0.05, 203.0)
    s = from_option_ticker(t)
    assert (s.bid, s.ask, s.last, s.iv, s.delta, s.theta, s.underlying_price) == (
        5.0,
        5.2,
        None,
        0.31,
        0.55,
        -0.05,
        203.0,
    )
    assert s.open_interest == 900.0


def test_option_params_map_expiries_and_strikes():
    from ib_async import OptionChain

    from stonks.execution.brokers.ibkr.ib_async_client import from_option_params

    p = from_option_params(OptionChain("SMART", 265598, "AAPL", "100", ["20261016"], [200.0]))
    assert (p.exchange, p.multiplier, p.expirations, p.strikes) == (
        "SMART",
        "100",
        ("20261016",),
        (200.0,),
    )
