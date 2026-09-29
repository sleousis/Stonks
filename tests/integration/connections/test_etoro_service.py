"""The ``etoro`` provider end to end, against a fake eToro (never the real
one): connect with keys, sync into the state DB, and trade through the
tick and ``open_trader``. Orders pass the order state machine, the stage
guard, the risk rules, halts and the kill switch like any broker's."""

from __future__ import annotations

import json
from datetime import date

import pytest

from stonks.accounts import Mode, PortfolioRepository, Scope, SubscriptionRepository
from stonks.connections.base import ProviderDisabled
from stonks.connections.ratelimit import reset_limiters
from stonks.connections.service import ConnectionService
from stonks.connections.settings import ConnectionsConfig
from stonks.core.protocols import SurvivalReport
from stonks.core.types import Order
from stonks.execution.brokers.base import StageRefusedError
from stonks.execution.brokers.etoro import client as etoro_client
from stonks.execution.brokers.etoro.instruments import reset_catalogs
from stonks.execution.cancel import cancel_working_orders
from stonks.execution.reconcile import reconcile_orders
from stonks.production.halts import trip_halt
from stonks.production.live.stages import change_stage
from stonks.production.tick import TickSettings, load_tick_plan, run_tick
from stonks.registry.store import StrategyRegistry
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from tests.fakes.etoro_server import API_KEY, USER_KEY, FakeEtoro, FakeInstrument
from tests.fixtures.governance import seed_status

DAY1, DAY2 = date(2026, 3, 17), date(2026, 3, 18)
SETTINGS = TickSettings(universe=["UP.US", "FLAT.US", "DOWN.US"], initial_cash=10_000.0)
KEYS = {"api_key": API_KEY, "user_key": USER_KEY}


@pytest.fixture(autouse=True)
def _fresh():
    reset_catalogs()
    reset_limiters()
    yield
    reset_catalogs()
    reset_limiters()


def etoro_fake(env: str = "demo") -> FakeEtoro:
    fake = FakeEtoro(env=env)
    for iid, symbol, price in [(3001, "UP", 190.0), (3002, "FLAT", 50.0), (3003, "DOWN", 70.0)]:
        fake.instruments[iid] = FakeInstrument(iid, symbol, exchange_id=5, bid=price, ask=price)
    return fake


def service_for(state, box, clock, fake: FakeEtoro, **etoro) -> ConnectionService:
    config = ConnectionsConfig(enabled_providers=("etoro",), etoro=etoro)
    return ConnectionService(state, config, box=box, clock=clock,
                             transports={"etoro": fake.transport()})  # fmt: skip


# ---- sync ------------------------------------------------------------------------------


def test_connect_and_sync_a_demo_account(state, box, clock, alice):
    fake = etoro_fake()
    fake.add_position(1001, 4, 150.0, opened="2026-02-02T15:00:00Z")
    fake.add_position(1, 1000, 1.1, settlement=0)  # a currency CFD: not covered
    svc = service_for(state, box, clock, fake)
    info = {p.name: p for p in svc.providers(alice)}["etoro"]
    assert info.enabled and info.has_paper and not info.can_trade  # trading is off
    record = svc.connect_with_keys(alice, "etoro", {**KEYS, "paper": "true"}, label="eToro")
    (acc,) = svc.accounts(alice, record.id)
    assert (acc.external_account_id, acc.name) == ("demo-4242", "eToro demo")
    assert acc.portfolio_id is not None

    result = svc.sync(alice, record.id)
    assert result.status == "ok"
    (pf,) = result.portfolios
    assert pf.unmapped == ("EURUSD",)
    snap = state.sql(
        "SELECT cash, positions_json FROM portfolio_snapshots WHERE id = ?", [pf.snapshot_id]
    )[0]
    assert snap["cash"] == pytest.approx(10_000.0)
    assert json.loads(snap["positions_json"]) == {"AAPL.US": 4.0}
    rows = state.sql(
        "SELECT kind, ticker, quantity FROM broker_activities WHERE portfolio_id = ?",
        [acc.portfolio_id],
    )
    assert {(r["kind"], r["ticker"], r["quantity"]) for r in rows} >= {("trade", "AAPL.US", 4.0)}
    # the keys are sealed, never stored in the clear
    sealed = state.sql("SELECT ciphertext FROM broker_credentials")[0]["ciphertext"]
    assert USER_KEY not in sealed and API_KEY not in sealed


def test_refused_keys_are_never_stored(state, box, clock, alice):
    fake = etoro_fake()
    svc = service_for(state, box, clock, fake)
    with pytest.raises(Exception) as info:
        svc.connect_with_keys(alice, "etoro", {"api_key": API_KEY, "user_key": "wrong-key-000"})
    assert "wrong-key-000" not in str(info.value)
    assert state.sql("SELECT COUNT(*) AS n FROM broker_credentials")[0]["n"] == 0


def test_no_etoro_code_runs_when_the_provider_is_off(state, box, clock, alice, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the eToro client must not be built")

    monkeypatch.setattr(etoro_client.EtoroClient, "__init__", boom)
    fake = etoro_fake()
    svc = ConnectionService(state, ConnectionsConfig(enabled_providers=("fake",)), box=box,
                            clock=clock, transports={"etoro": fake.transport()})  # fmt: skip
    info = {p.name: p for p in svc.providers(alice)}["etoro"]
    assert not info.enabled
    assert "etoro" not in {p.name for p in svc.available_providers(alice)}
    with pytest.raises(ProviderDisabled):
        svc.connect_with_keys(alice, "etoro", KEYS)
    svc.connect_with_keys(alice, "fake", {"token": "t-1"})
    svc.sync_due(Scope.service("scheduler"))
    assert fake.calls == []


# ---- trading ---------------------------------------------------------------------------


class World:
    """Bob links an eToro account and follows ``bh_up`` (UP.US) in auto."""

    def __init__(self, state, box, clock, lake, alice, fake: FakeEtoro, tmp, **etoro) -> None:
        self.state, self.lake, self.fake = state, lake, fake
        self.registry = StrategyRegistry(state=state, artifacts_dir=tmp / "art")
        self.registry.register(
            BuyAndHold({"ticker": "UP.US", "allocation": 0.4}),
            reports=[SurvivalReport(test_id="oos", passed=True, metrics={})],
            strategy_id="bh_up",
        )
        seed_status(self.registry, "bh_up", "active")
        self.svc = service_for(state, box, clock, fake, trading=True, **etoro)
        paper = "true" if fake.env == "demo" else "false"
        self.svc.connect_with_keys(alice, "etoro", {**KEYS, "paper": paper})
        [pf] = PortfolioRepository(state).list(alice)
        self.pid = pf.id
        self.scope = alice
        subs = SubscriptionRepository(state)
        sub = subs.subscribe(alice, strategy_id="bh_up", mode=Mode.PAPER, portfolio_id=self.pid)
        state.execute("UPDATE subscriptions SET mode = 'auto' WHERE id = ?", [sub.id])

    def trader(self):
        return self.svc.open_trader(self.scope, self.pid)

    def tick(self, as_of: date):
        plan = load_tick_plan(self.state, SETTINGS, traders=lambda account: self.trader())
        return run_tick(self.state, self.lake, self.registry, SETTINGS, as_of=as_of, plan=plan)

    def orders(self) -> list[dict]:
        rows = self.state.sql(
            "SELECT client_id, ticker, side, status, state, broker_order_id, status_reason"
            " FROM orders WHERE portfolio_id = ? ORDER BY created_at, client_id",
            [self.pid],
        )
        return [dict(r) for r in rows]

    def filled(self, client_id: str) -> float:
        rows = self.state.sql(
            "SELECT COALESCE(SUM(quantity), 0) AS q FROM fills WHERE order_client_id = ?",
            [client_id],
        )
        return float(rows[0]["q"])


@pytest.fixture
def world(state, box, clock, lake_trending, alice, tmp_path):
    return World(state, box, clock, lake_trending, alice, etoro_fake(), tmp_path)


def test_the_tick_opens_a_position_at_the_demo_account_and_books_the_fill(world):
    result = world.tick(DAY1)
    assert result.status == "ok"
    [order] = world.orders()
    assert (order["ticker"], order["side"], order["status"], order["state"]) == (
        "UP.US", "buy", "filled", "filled",
    )  # fmt: skip
    (sent,) = world.fake.orders.values()
    assert sent["settlementType"] == "real" and sent["requestType"] == "byUnits"
    assert world.filled(order["client_id"]) == pytest.approx(sent["requestedUnits"])
    assert order["broker_order_id"] == str(sent["orderId"])

    world.tick(DAY1)  # a same-day rerun sends nothing twice
    assert len(world.fake.orders) == 1


def test_a_partial_fill_books_what_executed(world):
    world.fake.fill_mode = "partial"
    world.tick(DAY1)
    [order] = world.orders()
    (sent,) = world.fake.orders.values()
    assert order["status"] == "cancelled"
    assert world.filled(order["client_id"]) == pytest.approx(sent["requestedUnits"] / 2)


def test_a_rejected_order_is_rejected_in_the_ledger(world):
    world.fake.fill_mode = "reject"
    world.tick(DAY1)
    [order] = world.orders()
    assert order["status"] == "rejected" and world.filled(order["client_id"]) == 0


def test_the_kill_switch_cancels_a_working_order_and_stops_new_ones(world):
    world.fake.fill_mode = "wait"
    world.tick(DAY1)
    [order] = world.orders()
    assert order["status"] == "pending" and order["state"] == "accepted"

    trip_halt(world.state, "kill", reason="stop", actor="t", portfolio_id=world.pid, halt="all")
    summary = cancel_working_orders(world.trader(), world.state, portfolio_id=world.pid)
    assert summary.cancelled == (order["client_id"],)
    [order] = world.orders()
    assert order["status"] == "cancelled"
    (sent,) = world.fake.orders.values()
    assert sent["status"] == 7

    world.tick(DAY2)  # halted: nothing new goes to eToro
    assert len(world.fake.orders) == 1


def test_a_fresh_process_reconciles_a_close_by_the_recorded_ids(world):
    world.tick(DAY1)
    [bought] = world.orders()
    units = world.filled(bought["client_id"])
    world.fake.fill_mode = "wait"
    trader = world.trader()
    sell = Order(client_id="close-1", ticker="UP.US", side="sell", quantity=units,
                 portfolio_id=world.pid)  # fmt: skip
    world.state.execute(
        "INSERT INTO orders (client_id, ticker, side, quantity, order_type, status, state,"
        " portfolio_id, created_at, updated_at) VALUES (?, 'UP.US', 'sell', ?, 'market',"
        " 'pending', 'pending', ?, 'x', 'x')",
        [sell.client_id, units, world.pid],
    )
    trader.place_order(sell)
    reconcile_orders(trader, world.state, portfolio_id=world.pid)
    row = world.state.sql("SELECT * FROM orders WHERE client_id = 'close-1'")[0]
    assert row["status"] == "pending" and row["broker_order_id"].startswith("close:")

    (close_id,) = world.fake.close_orders
    world.fake.release(close_id)
    reset_catalogs()
    reconcile_orders(world.trader(), world.state, portfolio_id=world.pid)  # a new trader
    row = world.state.sql("SELECT * FROM orders WHERE client_id = 'close-1'")[0]
    assert row["status"] == "filled"
    assert world.filled("close-1") == pytest.approx(units)
    assert world.fake.positions == []


def test_a_real_account_below_live_small_only_closes(
    state, box, clock, lake_trending, alice, tmp_path
):
    fake = etoro_fake(env="real")
    fake.add_position(3001, 5, 150.0)
    w = World(state, box, clock, lake_trending, alice, fake, tmp_path, allow_real_money=True)
    change_stage(state, w.pid, "broker_paper", actor="t", reason="setup",
                 gate_report={"target": "broker_paper", "passed": True})  # fmt: skip
    trader = w.trader()
    assert trader.real_money is True
    with pytest.raises(StageRefusedError):
        trader.place_order(Order(client_id="o-1", ticker="UP.US", side="buy", quantity=1))
    trader.place_order(Order(client_id="c-1", ticker="UP.US", side="sell", quantity=2))
    assert not fake.orders and len(fake.close_orders) == 1

    w.tick(DAY1)  # the tick's buy is refused the same way, and recorded as rejected
    [order] = [o for o in w.orders() if o["side"] == "buy"]
    assert order["status"] == "rejected" and "live_small" in (order["status_reason"] or "")
    assert not fake.orders
