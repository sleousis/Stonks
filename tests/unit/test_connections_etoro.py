"""The ``etoro`` connection provider: accounts, cash, positions and
activities from a fake eToro, and the trader's gates (trading off by
default, real money only when allowed, keys that cannot write)."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.connections.base import (
    Capability,
    CapabilityMissing,
    Credentials,
    ProviderAuthError,
    ProviderContext,
    ProviderError,
)
from stonks.connections.providers.etoro import EtoroConnection, env_of
from stonks.connections.ratelimit import reset_limiters
from stonks.connections.registry import provider_classes
from stonks.connections.settings import ConnectionsConfig
from stonks.core.types import Order
from stonks.execution.brokers.base import OrderRejectedError
from stonks.execution.brokers.etoro.instruments import reset_catalogs
from tests.fakes.etoro_server import API_KEY, USER_KEY, FakeEtoro


@pytest.fixture(autouse=True)
def _fresh():
    reset_catalogs()
    reset_limiters()
    yield
    reset_catalogs()
    reset_limiters()


def connect(fake: FakeEtoro, *, paper: str | None = None, **etoro) -> EtoroConnection:
    fields = {"api_key": API_KEY, "user_key": USER_KEY}
    if paper is not None:
        fields["paper"] = paper
    config = ConnectionsConfig(enabled_providers=("etoro",), etoro=etoro)
    context = ProviderContext(config=config, connection_id="con_1", transport=fake.transport(),
                              sleep=lambda s: None)  # fmt: skip
    return EtoroConnection.open(Credentials(fields), context)


def test_registered_with_keys_paper_and_trading_off_by_default():
    cls = provider_classes()["etoro"]
    assert cls is EtoroConnection
    assert cls.credential_fields == ("api_key", "user_key")
    assert cls.has_paper and not cls.needs_gateway and cls.auth_flow == "api_key"
    off = cls.capabilities_for(ConnectionsConfig())
    assert Capability.TRADE not in off and Capability.READ_POSITIONS in off
    on = cls.capabilities_for(ConnectionsConfig(etoro={"trading": True}))
    assert Capability.TRADE in on and Capability.SHORT not in on


def test_the_paper_flag_picks_the_environment():
    assert env_of(Credentials({})) == "demo"
    assert env_of(Credentials({"paper": "true"})) == "demo"
    assert env_of(Credentials({"paper": "false"})) == "real"


def test_accounts_name_the_environment_and_keep_no_personal_data():
    fake = FakeEtoro(env="demo")
    (acc,) = connect(fake).accounts()
    assert acc.id == "demo-4242"
    assert (acc.name, acc.currency, acc.institution, acc.number_mask) == (
        "eToro demo", "USD", "eToro", "…4242",
    )  # fmt: skip
    assert "Private" not in repr(acc) and "private-username" not in repr(acc)


def test_a_key_for_the_other_environment_is_refused_at_connect():
    fake = FakeEtoro(env="demo")  # the key is a demo key
    with pytest.raises(ProviderAuthError):
        connect(fake, paper="false").accounts()


def test_balances_follow_etoros_equity_formula():
    fake = FakeEtoro(credit=2_000.0)
    fake.add_position(1001, 10, 150.0)
    fake.positions[-1]["unrealizedPnL"]["pnL"] = 100.0
    conn = connect(fake)
    (acc,) = conn.accounts()
    bal = conn.balances(acc.id)
    assert (bal.currency, bal.cash, bal.buying_power) == ("USD", 2_000.0, 2_000.0)
    assert bal.total_value == pytest.approx(2_000.0 + 1_500.0 + 100.0)
    with pytest.raises(ProviderError):
        conn.balances("demo-1")


def test_positions_add_up_per_instrument_and_keep_uncovered_ones():
    fake = FakeEtoro()
    fake.add_position(1001, 3, 150.0)
    fake.add_position(1001, 2, 160.0)
    fake.add_position(1, 1000, 1.1, settlement=0)  # EURUSD: a CFD, not covered
    fake.add_position(1002, 4, 300.0, leverage=2)
    fake.mirrors.append({"mirrorID": 9, "positions": [dict(fake.positions[0], positionID=77,
                                                           mirrorID=9, units=1.0)]})  # fmt: skip
    conn = connect(fake)
    (acc,) = conn.accounts()
    by = {p.raw_symbol: p for p in conn.positions(acc.id)}
    assert by["AAPL"].ticker == "AAPL.US" and by["AAPL"].quantity == 6.0
    assert "copy trading" in (by["AAPL"].description or "")
    assert by["EURUSD"].ticker is None and "CFD" in (by["EURUSD"].description or "")
    assert by["MSFT"].ticker == "MSFT.US" and "leveraged" in (by["MSFT"].description or "")


def test_activities_are_opens_and_closes_for_tax_lots():
    fake = FakeEtoro()
    still_open = fake.add_position(1001, 3, 150.0, opened="2026-02-01T15:00:00Z")
    fake.history.append(
        {"positionId": 555, "instrumentId": 1002, "isBuy": True, "leverage": 1,
         "openRate": 300.0, "openTimestamp": "2026-01-10T15:00:00Z", "closeRate": 330.0,
         "closeTimestamp": "2026-02-20T15:00:00Z", "units": 2.0, "investment": 600.0,
         "netProfit": 60.0, "fees": 1.5}
    )  # fmt: skip
    conn = connect(fake)
    (acc,) = conn.accounts()
    acts = {a.provider_activity_id: a for a in conn.activities(acc.id, date(2026, 1, 1))}
    buy = acts[f"open:{still_open}"]
    assert (buy.ticker, buy.quantity, buy.price, buy.amount) == ("AAPL.US", 3.0, 150.0, -450.0)
    opened = acts["open:555"]
    assert (opened.ticker, opened.quantity, opened.trade_date) == (
        "MSFT.US",
        2.0,
        date(2026, 1, 10),
    )
    (close_id,) = [k for k in acts if k.startswith("close:555:")]
    close = acts[close_id]
    assert (close.quantity, close.price, close.amount, close.fee) == (-2.0, 330.0, 660.0, 1.5)
    assert all(a.kind == "trade" and a.currency == "USD" for a in acts.values())


def test_the_trader_is_refused_while_trading_is_off():
    conn = connect(FakeEtoro())
    (acc,) = conn.accounts()
    with pytest.raises(CapabilityMissing, match="trading = true"):
        conn.trader(acc.id)


def test_a_key_without_write_scope_cannot_trade():
    fake = FakeEtoro(scopes=["etoro-public:demo:read"])
    conn = connect(fake, trading=True)
    (acc,) = conn.accounts()
    with pytest.raises(ProviderError, match="Write permission"):
        conn.trader(acc.id)
    fake.scopes = ["etoro-public:demo:write"]
    assert connect(fake, trading=True).trader(acc.id).real_money is False


def test_real_money_opens_need_allow_real_money_but_closes_go_out():
    fake = FakeEtoro(env="real")
    fake.add_position(1001, 2, 150.0)
    conn = connect(fake, paper="false", trading=True)
    (acc,) = conn.accounts()
    trader = conn.trader(acc.id)
    assert trader.real_money is True
    with pytest.raises(OrderRejectedError, match="allow_real_money"):
        trader.place_order(Order(client_id="b1", ticker="AAPL.US", side="buy", quantity=1))
    trader.place_order(Order(client_id="s1", ticker="AAPL.US", side="sell", quantity=2))
    assert len(fake.close_orders) == 1 and not fake.orders

    allowed = connect(fake, paper="false", trading=True, allow_real_money=True)
    allowed.accounts()
    allowed.trader(acc.id).place_order(
        Order(client_id="b2", ticker="AAPL.US", side="buy", quantity=1)
    )
    assert len(fake.orders) == 1


def test_errors_never_carry_the_keys():
    fake = FakeEtoro()
    fake.queue("/api/v1/me", 400, body={"errorMessage": f"bad {API_KEY} {USER_KEY}"})
    with pytest.raises(ProviderError) as info:
        connect(fake).accounts()
    assert API_KEY not in str(info.value) and USER_KEY not in str(info.value)
    assert "api_key" not in repr(Credentials({"api_key": API_KEY})) or API_KEY not in repr(
        Credentials({"api_key": API_KEY})
    )
