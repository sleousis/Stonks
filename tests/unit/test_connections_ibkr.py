"""The ``ibkr`` connection provider (roadmap 19.3) over ``FakeIbGateway``:
configuration, reads (accounts, balances, positions, activities), Flex
activities, error mapping, the shared sessions and ``trader()``. No network."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from stonks.connections.base import (
    Capability,
    Credentials,
    ProviderAuthError,
    ProviderContext,
    ProviderError,
    ProviderNotConfigured,
    ProviderUnavailable,
)
from stonks.connections.providers import ibkr as provider
from stonks.connections.providers.ibkr import IbkrConnection
from stonks.connections.registry import provider_class
from stonks.connections.settings import ConnectionsConfig
from stonks.core.types import Order
from stonks.execution.brokers.base import OrderRejectedError
from stonks.execution.brokers.ibkr.broker import IbkrBroker
from stonks.execution.brokers.ibkr.client import IbExecution
from stonks.execution.brokers.ibkr.flex import FlexClient
from stonks.execution.brokers.ibkr.settings import IbkrFlexSettings
from tests.fakes.ib_gateway import AAPL, BRKB, MSFT, T0, VOD, FakeIbGateway, stock

ACCOUNT = "DU1234567"
TOYOTA = stock(4321, "7203", currency="JPY", primary="TSEJ")

CONFIG = ConnectionsConfig(
    enabled_providers=("ibkr",),
    ibkr={
        "gateways": {
            "paper": {"host": "gw-p", "port": 4004, "mode": "paper", "account_id": ACCOUNT},
            "other": {"host": "gw-o", "port": 4005, "mode": "paper"},
        }
    },
)


@pytest.fixture(autouse=True)
def _fresh_sessions():
    provider.reset_sessions()
    provider.clear_flex_cache()
    yield
    provider.reset_sessions()
    provider.clear_flex_cache()


@pytest.fixture
def gw() -> FakeIbGateway:
    g = FakeIbGateway([ACCOUNT])
    g.set_values(
        NetLiquidation="100000", TotalCashValue="40000", BuyingPower="40000",
        SettledCash="40000", AvailableFunds="40000",
    )  # fmt: skip
    return g


def factory_for(gw: FakeIbGateway):
    """One client factory per fake gateway, recording the endpoints asked."""
    if not hasattr(gw, "factory"):
        gw.endpoints = []  # type: ignore[attr-defined]

        def factory(endpoint):
            gw.endpoints.append(endpoint)  # type: ignore[attr-defined]
            return gw

        gw.factory = factory  # type: ignore[attr-defined]
    return gw.factory  # type: ignore[attr-defined]


def opened(gw: FakeIbGateway, *, gateway: str = "paper", **extra) -> IbkrConnection:
    ctx = ProviderContext(config=CONFIG, connection_id="c1", transport=factory_for(gw), extra=extra)
    return IbkrConnection.open(Credentials({"gateway": gateway}), ctx)


def test_is_registered_with_trade_and_short():
    cls = provider_class("ibkr")
    assert cls is IbkrConnection
    assert cls.auth_flow == "api_key"
    assert cls.credential_fields == ("gateway",)
    assert cls.has_paper
    for cap in (Capability.READ_BALANCES, Capability.READ_POSITIONS, Capability.READ_ACTIVITY,
                Capability.TRADE, Capability.SHORT):  # fmt: skip
        assert cls.supports(cap)


def test_needs_a_configured_gateway():
    with pytest.raises(ProviderNotConfigured, match=r"brokers.ibkr.gateways"):
        IbkrConnection.check_configured(ConnectionsConfig(enabled_providers=("ibkr",)))
    IbkrConnection.check_configured(CONFIG)
    ctx = ProviderContext(config=CONFIG, transport=factory_for(FakeIbGateway()))
    with pytest.raises(ProviderError, match="nope"):
        IbkrConnection.open(Credentials({"gateway": "nope"}), ctx)


def test_accounts_and_balances_read_the_checked_account(gw):
    conn = opened(gw)
    (acc,) = conn.accounts()
    assert acc.id == ACCOUNT
    assert acc.number_mask == "…4567"
    assert acc.institution == "Interactive Brokers"
    assert "paper" in acc.name
    bal = conn.balances(ACCOUNT)
    assert (bal.currency, bal.cash, bal.buying_power, bal.total_value) == (
        "USD", 40000.0, 40000.0, 100000.0,
    )  # fmt: skip
    assert gw.endpoints[0].client_id == 12  # the sync role
    with pytest.raises(ProviderError, match="unknown"):
        conn.balances("U999")


def test_positions_map_to_tickers_and_keep_uncovered_ones(gw):
    gw.set_position(AAPL, 10)
    gw.set_position(BRKB, 2)
    gw.set_position(TOYOTA, 100)
    gw.set_position(MSFT, 0)
    positions = {p.raw_symbol: p for p in opened(gw).positions(ACCOUNT)}
    assert set(positions) == {"AAPL", "BRK B", "7203"}
    assert positions["AAPL"].ticker == "AAPL.US"
    assert positions["BRK B"].ticker == "BRK-B.US"
    assert positions["7203"].ticker is None  # not covered, never dropped
    assert positions["7203"].currency == "JPY"


def test_activities_include_every_execution_manual_ones_too(gw):
    b = IbkrBroker(gw, mode="paper")
    b.place_order(Order(client_id="t1-s1-AAPL.US-buy", ticker="AAPL.US", side="buy",
                        quantity=10, decision_price=200.0))  # fmt: skip
    exec_id = gw.fill("t1-s1-AAPL.US-buy", 10, 200.0, commission=1.0)
    manual = gw.add_manual_order(MSFT, 5)
    gw._executions.append(  # a hand-placed sell: no orderRef
        gw._executions[0].__class__(
            exec_id="0002.0001", order_ref="", perm_id=manual.perm_id, contract=MSFT.contract,
            side="SLD", shares=5, price=300.0, time=T0, account=ACCOUNT,
        )
    )  # fmt: skip
    acts = {a.provider_activity_id: a for a in opened(gw).activities(ACCOUNT, T0.date())}
    buy = acts[f"exec:{exec_id}"]
    assert (buy.kind, buy.ticker, buy.quantity, buy.price, buy.fee) == (
        "trade", "AAPL.US", 10.0, 200.0, 1.0,
    )  # fmt: skip
    assert buy.amount == pytest.approx(-2001.0)
    sell = acts["exec:0002.0001"]
    assert (sell.ticker, sell.quantity, sell.amount) == ("MSFT.US", -5.0, 1500.0)
    assert opened(gw).activities(ACCOUNT, T0.date() + timedelta(days=1)) == []


def test_london_activities_are_in_pounds(gw):
    # IBKR reports a London execution in pence (magnifier 100, roadmap 19.16)
    manual = gw.add_manual_order(VOD, 5)
    gw._executions.append(
        IbExecution(
            exec_id="0003.0001", order_ref="", perm_id=manual.perm_id, contract=VOD.contract,
            side="SLD", shares=5, price=150.0, time=T0, account=ACCOUNT,
        )
    )  # fmt: skip
    acts = {a.provider_activity_id: a for a in opened(gw).activities(ACCOUNT, T0.date())}
    sell = acts["exec:0003.0001"]
    assert (sell.ticker, sell.price, sell.currency) == ("VOD.LSE", 1.5, "GBP")
    assert sell.amount == pytest.approx(7.5)


FLEX = """<FlexQueryResponse queryName="q" type="AF"><FlexStatements count="1">
<FlexStatement accountId="DU1234567" fromDate="20260901" toDate="20260925">
<Trades>
<Trade accountId="DU1234567" currency="USD" assetCategory="STK" symbol="BRK B" conid="72063691"
  tradeID="9002" ibExecID="0001f4e8.2" tradeDate="20260924" settleDateTarget="20260925"
  quantity="-3" tradePrice="450" proceeds="1350" ibCommission="-0.5" ibCommissionCurrency="USD"
  listingExchange="NYSE" levelOfDetail="EXECUTION"/>
</Trades>
<CashTransactions>
<CashTransaction accountId="DU1234567" currency="USD" type="Dividends" amount="2.5"
  dateTime="20260915" transactionID="7001" symbol="AAPL" conid="265598" description="DIV"/>
<CashTransaction accountId="DU1234567" currency="USD" type="Withholding Tax" amount="-0.38"
  dateTime="20260915" transactionID="7003" symbol="AAPL" conid="265598"/>
<CashTransaction accountId="DU1234567" currency="USD" type="Deposits/Withdrawals" amount="-100"
  dateTime="20260902" transactionID="7002"/>
<CashTransaction accountId="DU1234567" currency="USD" type="Broker Interest Received"
  amount="4.2" dateTime="20260903" transactionID="7004"/>
<CashTransaction accountId="U0000001" currency="USD" type="Dividends" amount="9"
  dateTime="20260915" transactionID="7999"/>
</CashTransactions>
</FlexStatement></FlexStatements></FlexQueryResponse>"""

SEND = ("<FlexStatementResponse><Status>Success</Status><ReferenceCode>R</ReferenceCode>"
        "</FlexStatementResponse>")  # fmt: skip


def flex_client(calls: list) -> FlexClient:
    answers = [SEND, FLEX]

    def transport(url, params):
        calls.append(url)
        return answers[(len(calls) - 1) % 2]

    return FlexClient("tok-123456789", IbkrFlexSettings(query_id="1"), transport=transport,
                      sleep=lambda s: None)  # fmt: skip


def test_flex_adds_older_trades_and_cash_transactions(gw):
    calls: list = []
    conn = opened(gw, flex=flex_client(calls))
    acts = {a.provider_activity_id: a for a in conn.activities(ACCOUNT, date(2026, 9, 1))}
    assert set(acts) == {"exec:0001f4e8.2", "cash:7001", "cash:7003", "cash:7002", "cash:7004"}
    trade = acts["exec:0001f4e8.2"]
    assert (trade.ticker, trade.quantity, trade.fee, trade.amount) == (
        "BRK-B.US", -3.0, 0.5, 1349.5,
    )  # fmt: skip
    assert trade.settle_date == date(2026, 9, 25)
    assert (acts["cash:7001"].kind, acts["cash:7001"].ticker) == ("dividend", "AAPL.US")
    assert acts["cash:7003"].kind == "other"  # withholding tax
    assert acts["cash:7002"].kind == "withdrawal"
    assert acts["cash:7004"].kind == "interest"
    # the statement is cached: a second sync does not fetch it again
    opened(gw, flex=flex_client(calls)).activities(ACCOUNT, date(2026, 9, 1))
    assert len(calls) == 2
    later = opened(gw, flex=flex_client(calls)).activities(ACCOUNT, date(2026, 9, 10))
    assert {a.provider_activity_id for a in later} == {"exec:0001f4e8.2", "cash:7001", "cash:7003"}


def test_a_flex_failure_never_fails_the_sync(gw):
    def broken(url, params):
        raise OSError("down")

    client = FlexClient("tok-123456789", IbkrFlexSettings(query_id="2"), transport=broken)
    assert opened(gw, flex=client).activities(ACCOUNT, T0.date()) == []


def test_gateway_faults_map_to_provider_errors(gw):
    gw.connect_failures = 1
    with pytest.raises(ProviderUnavailable):
        opened(gw).accounts()
    provider.reset_sessions()
    wrong = FakeIbGateway(["DU7654321"])
    with pytest.raises(ProviderAuthError, match="another account"):
        opened(wrong).accounts()
    provider.reset_sessions()
    competing = FakeIbGateway([ACCOUNT])
    competing.competing = True
    with pytest.raises(ProviderUnavailable, match="competing"):
        opened(competing).balances(ACCOUNT)


def test_sessions_are_shared_and_never_closed_by_a_connection(gw):
    first = opened(gw)
    first.accounts()
    first.close()
    second = opened(gw)
    second.accounts()
    assert len(gw.endpoints) == 1  # the second reused the open session
    assert gw.connected


def test_trader_is_an_ibkr_broker_bound_to_the_account(gw, state):
    conn = opened(gw, state=state)
    trader = conn.trader(ACCOUNT)
    assert isinstance(trader, IbkrBroker)
    assert gw.endpoints[-1].client_id == 11  # the tick role
    assert trader.account_id == ACCOUNT
    assert (trader.account_type, trader.allow_short) == ("cash", False)
    with pytest.raises(OrderRejectedError, match="long only"):
        trader.place_order(Order(client_id="t1-s1-AAPL.US-sell", ticker="AAPL.US", side="sell",
                                 quantity=1, position_effect="open", decision_price=1.0))  # fmt: skip
    with pytest.raises(ProviderError, match="U999"):
        conn.trader("U999")


def test_trader_without_a_configured_account_checks_the_one_it_was_given(state):
    g = FakeIbGateway(["DU2222222"])
    conn = opened(g, gateway="other", state=state)
    trader = conn.trader("DU2222222")
    assert trader.account_id == "DU2222222"
    provider.reset_sessions()
    bad = opened(FakeIbGateway(["DU3333333"]), gateway="other", state=state).trader("DU2222222")
    from stonks.execution.brokers.base import LiveTradingRefusedError

    with pytest.raises(LiveTradingRefusedError):
        bad.fetch_portfolio()
