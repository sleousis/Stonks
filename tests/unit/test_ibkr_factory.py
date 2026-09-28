"""Settings onto an ``IbkrBroker`` (roadmap 19.2)."""

from __future__ import annotations

import pytest

from stonks.execution.brokers.base import BrokerError
from stonks.execution.brokers.ibkr.contracts import MemoryContractCache, SqliteContractCache
from stonks.execution.brokers.ibkr.factory import (
    connect_ibkr,
    endpoint_for,
    order_ref_lookup,
    pick_gateway,
)
from stonks.execution.brokers.ibkr.settings import IbkrBrokerConfig
from tests.fakes.ib_gateway import FakeIbGateway

CONFIG = IbkrBrokerConfig(
    allow_live=True,
    gateways={
        "paper": {"host": "gw-p", "port": 4004, "mode": "paper", "portfolios": ["pf_a"]},
        "live": {
            "host": "gw-l",
            "port": 4003,
            "mode": "live",
            "portfolios": ["pf_b"],
            "account_id": "U1",
        },
    },
)


def test_pick_gateway():
    assert pick_gateway(CONFIG, gateway="live")[0] == "live"
    assert pick_gateway(CONFIG, portfolio_id="pf_a")[0] == "paper"
    with pytest.raises(BrokerError, match="named"):
        pick_gateway(CONFIG, gateway="nope")
    with pytest.raises(BrokerError, match="none lists"):
        pick_gateway(CONFIG, portfolio_id="pf_z")
    with pytest.raises(BrokerError, match="no IB Gateway"):
        pick_gateway(IbkrBrokerConfig())
    one = IbkrBrokerConfig(gateways={"p": {"host": "h", "port": 1, "mode": "paper"}})
    assert pick_gateway(one, portfolio_id="anything")[0] == "p"


def test_endpoint_uses_the_role_client_id():
    _, gw = pick_gateway(CONFIG, gateway="paper")
    assert endpoint_for(CONFIG, gw, "tick").client_id == 11
    assert endpoint_for(CONFIG, gw, "sync").client_id == 12
    health = endpoint_for(CONFIG, gw, "health")
    assert (health.client_id, health.readonly, health.host, health.port) == (13, True, "gw-p", 4004)
    assert (health.connect_timeout, health.reconnect_deadline) == (2.0, 0.0)


def test_connect_ibkr_wires_the_gateway_settings(state):
    seen = []

    def factory(endpoint):
        seen.append(endpoint)
        return FakeIbGateway(["U1"])

    broker = connect_ibkr(CONFIG, portfolio_id="pf_b", state=state, client_factory=factory)
    assert seen[0].port == 4003
    assert (broker.mode, broker.expected_account, broker.allow_live) == ("live", "U1", True)
    assert isinstance(broker.resolver.cache, SqliteContractCache)
    assert broker.account_id == "U1"

    memory = connect_ibkr(CONFIG, gateway="paper", client_factory=lambda e: FakeIbGateway())
    assert isinstance(memory.resolver.cache, MemoryContractCache)


def test_the_journal_records_each_gateway_and_role_apart(tmp_path):
    from stonks.execution.brokers.ibkr.journal import JournalingIbClient

    config = CONFIG.model_copy(update={"journal": {"enabled": True, "dir": tmp_path}}, deep=True)
    config = type(CONFIG).model_validate(config.model_dump())
    broker = connect_ibkr(
        config, gateway="paper", role="sync", client_factory=lambda e: FakeIbGateway()
    )
    assert isinstance(broker.client, JournalingIbClient)
    broker.ensure_ready()
    files = list((tmp_path / "paper" / "sync").glob("*.jsonl"))
    assert len(files) == 1 and files[0].read_text(encoding="utf-8").strip()
    off = connect_ibkr(CONFIG, gateway="paper", client_factory=lambda e: FakeIbGateway())
    assert not isinstance(off.client, JournalingIbClient)


def test_order_ref_lookup(state):
    state.execute(
        "INSERT INTO orders (client_id, ticker, side, quantity, order_type, status, broker_ref,"
        " created_at, updated_at) VALUES ('long-id', 'AAPL.US', 'buy', 1, 'market', 'pending',"
        " 'stk-abc', 'x', 'x')"
    )
    lookup = order_ref_lookup(state)
    assert lookup("stk-abc") == "long-id"
    assert lookup("stk-zzz") is None


def test_close_runs_owned_closers():
    closed = []

    class Closable(FakeIbGateway):
        def close(self):
            closed.append("client")

    broker = connect_ibkr(CONFIG, gateway="paper", client_factory=lambda e: Closable())
    broker.on_close(lambda: closed.append("state"))
    broker.close()
    broker.close()
    assert closed == ["client", "state", "client"]


def test_a_cash_gateway_trades_long_only_and_a_margin_gateway_checks_borrow():
    from stonks.execution.borrow import FlatBorrow
    from stonks.execution.brokers.ibkr.borrow import IbkrBorrowSource

    cash = connect_ibkr(CONFIG, gateway="paper", client_factory=lambda ep: FakeIbGateway())
    assert (cash.account_type, cash.allow_short, cash.borrow) == ("cash", False, None)
    margin_cfg = IbkrBrokerConfig(
        gateways={"m": {"host": "h", "port": 1, "mode": "paper", "account_type": "margin"}}
    )
    fees = FlatBorrow()
    margin = connect_ibkr(margin_cfg, client_factory=lambda ep: FakeIbGateway(), borrow_fees=fees)
    assert (margin.account_type, margin.allow_short) == ("margin", True)
    assert isinstance(margin.borrow, IbkrBorrowSource)
    with pytest.raises(ValueError, match="account_type"):
        IbkrBrokerConfig(gateways={"m": {"host": "h", "port": 1, "mode": "paper",
                                         "account_type": "cfd"}})  # fmt: skip


def test_connect_ibkr_reads_the_portfolio_stage(state):
    from stonks.core.types import Order
    from stonks.execution.brokers.base import LiveTradingRefusedError
    from stonks.production.live.stages import change_stage

    fake = FakeIbGateway(["U1"])
    live = CONFIG.gateways["live"].model_copy(update={"portfolios": ["pf_default"]})
    config = CONFIG.model_copy(update={"gateways": {**CONFIG.gateways, "live": live}})
    broker = connect_ibkr(
        config, portfolio_id="pf_default", state=state, client_factory=lambda e: fake
    )
    order = Order(client_id="c1", ticker="AAPL.US", side="buy", quantity=1.0, decision_price=10.0)
    with pytest.raises(LiveTradingRefusedError, match="sim_paper"):
        broker.place_order(order)
    for stage in ("broker_paper", "live_small"):
        change_stage(
            state, "pf_default", stage, actor="u", reason="r",
            gate_report={"target": stage, "passed": True},
        )  # fmt: skip
    broker.place_order(order)
    assert fake.sent_count("c1") == 1
    # a gateway picked by name that serves one portfolio reads that one
    named = connect_ibkr(config, gateway="live", state=state, client_factory=lambda e: fake)
    assert named._stage_lookup is not None and named._stage_lookup() == "live_small"
