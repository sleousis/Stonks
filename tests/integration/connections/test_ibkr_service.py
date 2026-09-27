"""The ``ibkr`` provider through ``ConnectionService`` (roadmap 19.3):
connect by gateway name, sync positions, cash and activities into the
state DB, and trade through ``open_trader``. The account is shared with
the owner's manual trading, so the book owns only what it opened."""

from __future__ import annotations

import json

import pytest

from stonks.connections.providers import ibkr as provider
from stonks.connections.service import ConnectionService
from stonks.connections.settings import ConnectionsConfig
from stonks.core.types import Order
from stonks.execution.brokers.ibkr.broker import IbkrBroker
from stonks.execution.brokers.ibkr.contracts import SqliteContractCache
from stonks.execution.reconcile import reconcile_orders
from stonks.production.ownership import managed_view, owned_positions
from tests.fakes.ib_gateway import AAPL, MSFT, FakeIbGateway, stock

ACCOUNT = "DU1234567"
TOYOTA = stock(4321, "7203", currency="JPY", primary="TSEJ")


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
    g.set_values(NetLiquidation="100000", TotalCashValue="40000", BuyingPower="40000")
    g.set_position(MSFT, 7)  # the owner's own holding, bought by hand
    g.set_position(TOYOTA, 100)  # a market Stonks does not cover
    return g


@pytest.fixture
def ibkr_service(state, box, clock, gw) -> ConnectionService:
    config = ConnectionsConfig(
        enabled_providers=("ibkr",),
        ibkr={"gateways": {"paper": {"host": "gw", "port": 4004, "mode": "paper",
                                     "account_id": ACCOUNT}}},
    )  # fmt: skip
    return ConnectionService(state, config, box=box, clock=clock,
                             transports={"ibkr": lambda endpoint: gw})  # fmt: skip


def test_ibkr_is_offered_only_with_a_gateway(state, box, alice):
    bare = ConnectionService(state, ConnectionsConfig(enabled_providers=("ibkr",)), box=box)
    assert [p.name for p in bare.available_providers(alice)] == []
    info = {p.name: p for p in bare.providers(alice)}["ibkr"]
    assert info.can_trade and info.has_paper and info.credential_fields == ("gateway",)


def test_connect_sync_and_trade_with_ownership_by_attribution(ibkr_service, alice, state, gw):
    record = ibkr_service.connect_with_keys(alice, "ibkr", {"gateway": "paper"}, label="IBKR")
    (acc,) = ibkr_service.accounts(alice, record.id)
    assert (acc.external_account_id, acc.number_mask) == (ACCOUNT, "…4567")
    pid = acc.portfolio_id
    assert pid is not None

    result = ibkr_service.sync(alice, record.id)
    assert result.status == "ok"
    (pf,) = result.portfolios
    assert pf.unmapped == ("7203",)
    snap = state.sql(
        "SELECT cash, total_value, positions_json FROM portfolio_snapshots WHERE id = ?",
        [pf.snapshot_id],
    )[0]
    assert (snap["cash"], snap["total_value"]) == (40000.0, 100000.0)
    assert json.loads(snap["positions_json"]) == {"MSFT.US": 7.0}

    # the book trades through the connection
    trader = ibkr_service.open_trader(alice, pid)
    assert isinstance(trader, IbkrBroker)
    assert isinstance(trader.resolver.cache, SqliteContractCache)
    order = Order(client_id="t1-s1-AAPL.US-buy", ticker="AAPL.US", side="buy", quantity=10,
                  decision_price=200.0)  # fmt: skip
    state.execute(
        "INSERT INTO orders (client_id, tick_id, strategy_id, ticker, side, quantity,"
        " order_type, status, state, portfolio_id, created_at, updated_at)"
        " VALUES (?, NULL, NULL, 'AAPL.US', 'buy', 10, 'market', 'pending', 'pending', ?,"
        " 'x', 'x')",
        [order.client_id, pid],
    )
    trader.place_order(order)
    gw.fill(order.client_id, 10, 200.0, commission=1.0)
    gw.set_position(AAPL, 10)
    reconcile_orders(trader, state, portfolio_id=pid)

    # the book owns AAPL only: the owner's MSFT stays external, never traded
    managed, external = managed_view(trader.fetch_portfolio(), owned_positions(state, pid))
    assert managed.positions == {"AAPL.US": 10.0}
    assert external == {"MSFT.US": 7.0, "7203": 100.0}

    # the next sync books the execution as an activity of the account
    again = ibkr_service.sync(alice, record.id)
    assert again.portfolios[0].activities_new == 1
    row = state.sql(
        "SELECT kind, ticker, quantity, fee FROM broker_activities WHERE portfolio_id = ?", [pid]
    )[0]
    assert (row["kind"], row["ticker"], row["quantity"], row["fee"]) == (
        "trade", "AAPL.US", 10.0, 1.0,
    )  # fmt: skip


def test_a_wrong_account_is_an_auth_error_that_pauses_the_connection(
    ibkr_service, alice, state, gw
):
    record = ibkr_service.connect_with_keys(alice, "ibkr", {"gateway": "paper"})
    provider.reset_sessions()
    gw.accounts = ["DU7654321"]
    result = ibkr_service.sync(alice, record.id)
    assert result.status == "error"
    assert ibkr_service.get(alice, record.id).status == "error"
