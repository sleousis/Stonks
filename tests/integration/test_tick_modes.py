"""Automation modes in the tick (roadmap 15.5, S6): one tick serves notify,
paper and auto subscriptions of several traders. Paper trades a simulated
account per portfolio, auto trades the portfolio's connected account and
pauses itself on a broker error, the kill switch works at global, user and
portfolio scope, owners' risk limits tighten their books, and every book
writes one ``portfolio_runs`` row, which the paper-day count reads."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.accounts import (
    Mode,
    PortfolioRepository,
    Role,
    Scope,
    SubscriptionRepository,
    UserRepository,
)
from stonks.accounts.paper import paper_account_id
from stonks.connections.base import ProviderError
from stonks.connections.providers import fake
from stonks.connections.ratelimit import reset_limiters
from stonks.connections.service import ConnectionService
from stonks.connections.settings import ConnectionsConfig
from stonks.core.protocols import SurvivalReport
from stonks.production.halts import trip_halt
from stonks.production.portfolio_runs import list_runs
from stonks.production.tick import TickSettings, load_tick_plan, run_tick
from stonks.registry.store import StrategyRegistry
from stonks.security import KeyRing, SecretBox, generate_key
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from tests.fixtures.governance import seed_status

DAY1, DAY2, DAY3 = date(2026, 3, 17), date(2026, 3, 18), date(2026, 3, 19)
UNIVERSE = ["UP.US", "FLAT.US", "DOWN.US"]
SETTINGS = TickSettings(universe=UNIVERSE, initial_cash=10_000.0)
TOKEN = "tok-bob"
SCHEDULER = Scope.service("scheduler")


@pytest.fixture(autouse=True)
def _clean_fakes():
    fake.FAKE_BOOKS.clear()
    reset_limiters()
    yield
    fake.FAKE_BOOKS.clear()
    reset_limiters()


class World:
    """Alice: a simulated portfolio (paper). Bob: a broker portfolio on a
    fake_trading connection with an auto and a paper subscription. Carol:
    notify only."""

    def __init__(self, tmp_path, lake) -> None:
        self.lake = lake
        self.state = state = SqliteState(tmp_path / "state.sqlite")
        state.migrate()
        self.registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
        reports = [SurvivalReport(test_id="oos", passed=True, metrics={})]
        for sid, ticker in [("bh_up", "UP.US"), ("bh_down", "DOWN.US"), ("bh_flat", "FLAT.US")]:
            self.registry.register(
                BuyAndHold({"ticker": ticker, "allocation": 0.4}), reports=reports, strategy_id=sid
            )
            seed_status(self.registry, sid, "active")
        users = UserRepository(state)
        self.users = users
        self.subs = SubscriptionRepository(state)
        portfolios = PortfolioRepository(state)

        def person(name: str) -> Scope:
            return Scope.for_user(users.create(display_name=name, role=Role.TRADER, actor="t"))

        self.alice, self.bob, self.carol = person("Alice"), person("Bob"), person("Carol")
        self.sim = portfolios.create(self.alice, name="Sim").id
        self.alice_paper = self.subs.subscribe(
            self.alice, strategy_id="bh_up", mode=Mode.PAPER, portfolio_id=self.sim
        ).id

        box = SecretBox(KeyRing.parse(f"k1:{generate_key()}"))
        self.connections = ConnectionService(
            state, ConnectionsConfig(enabled_providers=("fake_trading",)), box=box
        )
        self.connections.connect_with_keys(self.bob, "fake_trading", {"token": TOKEN})
        [live] = portfolios.list(self.bob)
        self.live = live.id
        self.book = fake.book_for(TOKEN)
        [account] = self.book.accounts
        self.book.positions = {account.id: []}
        self.book.quotes = {"UP.US": 150.0, "DOWN.US": 80.0, "FLAT.US": 50.0}
        self.bob_auto = self.subs.subscribe(
            self.bob, strategy_id="bh_up", mode=Mode.PAPER, portfolio_id=self.live
        ).id
        state.execute("UPDATE subscriptions SET mode = 'auto' WHERE id = ?", [self.bob_auto])
        self.bob_paper = self.subs.subscribe(
            self.bob, strategy_id="bh_down", mode=Mode.PAPER, portfolio_id=self.live
        ).id
        self.subs.subscribe(self.carol, strategy_id="bh_flat", mode=Mode.NOTIFY)

    def traders(self, account):
        return self.connections.open_trader(SCHEDULER, account.id)

    def tick(self, as_of: date, **kw):
        plan = load_tick_plan(self.state, SETTINGS, traders=self.traders)
        return run_tick(
            self.state, self.lake, self.registry, SETTINGS, as_of=as_of, plan=plan, **kw
        )

    def orders(self, portfolio_id: str) -> list[dict]:
        rows = self.state.sql(
            "SELECT client_id, strategy_id, ticker, side, status FROM orders"
            " WHERE portfolio_id = ? ORDER BY client_id",
            [portfolio_id],
        )
        return [dict(r) for r in rows]


@pytest.fixture
def world(tmp_path, lake_trending):
    w = World(tmp_path, lake_trending)
    yield w
    w.state.close()


def test_one_tick_serves_notify_paper_and_auto(world):
    result = world.tick(DAY1)
    assert result.status == "ok"
    paper_account = paper_account_id(world.live)

    # paper: Alice's simulated portfolio, and Bob's paper account; never Bob's broker
    [alice_order] = world.orders(world.sim)
    assert alice_order["client_id"] == f"2026-03-17:{world.sim}:bh_up:UP.US:buy"
    assert alice_order["status"] == "filled"
    [paper_order] = world.orders(paper_account)
    assert (paper_order["strategy_id"], paper_order["ticker"]) == ("bh_down", "DOWN.US")
    # auto: through the connection, booked from the broker's own state
    [auto_order] = world.orders(world.live)
    assert auto_order["client_id"] == f"2026-03-17:{world.live}:bh_up:UP.US:buy"
    assert auto_order["status"] == "filled"
    assert list(world.book.orders) == [auto_order["client_id"]]
    assert "place_order" in world.book.calls
    # notify: Carol's signal waits in the outbox, nothing placed for her
    [outbox] = world.state.sql("SELECT * FROM notification_outbox WHERE category = 'signal'")
    assert outbox["strategy_id"] == "bh_flat" and outbox["user_id"] == world.carol.user_id

    runs = {pid: list_runs(world.state, pid) for pid in (world.sim, world.live, paper_account)}
    [sim_run], [live_run], [paper_run] = runs.values()
    assert (sim_run.mode, sim_run.status, sim_run.orders_placed) == ("paper", "ok", 1)
    assert sim_run.paper_subscriptions == (world.alice_paper,)
    assert (live_run.mode, live_run.auto_subscriptions) == ("auto", (world.bob_auto,))
    assert (paper_run.mode, paper_run.paper_subscriptions) == ("paper", (world.bob_paper,))
    assert world.subs.get(world.alice, world.alice_paper).paper_days_completed == 1
    assert world.subs.get(world.bob, world.bob_paper).paper_days_completed == 1

    world.tick(DAY1)  # a same-day re-run places nothing twice
    assert len(world.book.orders) == 1 and len(world.orders(world.sim)) == 1
    assert world.subs.get(world.alice, world.alice_paper).paper_days_completed == 1
    world.tick(DAY2)
    assert world.subs.get(world.alice, world.alice_paper).paper_days_completed == 2


def test_a_broker_error_pauses_auto_and_the_other_books_go_on(world):
    world.tick(DAY1)
    world.book.fail = ProviderError("fake broker is down", status=503)
    result = world.tick(DAY2)

    assert result.status == "partial"
    auto = world.subs.get(world.bob, world.bob_auto)
    assert auto.mode is Mode.AUTO
    assert auto.paused_reason is not None and auto.paused_reason.startswith("broker_error: ")
    [live] = [r for r in result.portfolios if r.portfolio_id == world.live]
    assert live.status == "error" and live.summary["auto_paused"] == [world.bob_auto]
    [run] = [r for r in list_runs(world.state, world.live) if r.as_of == DAY2]
    assert run.status == "paused" and run.error is not None
    [audit] = world.state.sql("SELECT * FROM audit_log WHERE action = 'subscription.auto_paused'")
    assert audit["actor"] == "service:system" and audit["target_id"] == world.bob_auto
    [notice] = world.state.sql("SELECT * FROM notification_outbox WHERE category = 'risk'")
    assert notice["user_id"] == world.bob.user_id
    # the paper books were untouched by Bob's broker
    assert all(
        r.status in ("ok", "noop") for r in result.portfolios if r.portfolio_id != world.live
    )

    # paused: the next tick has no auto book and calls the broker no more
    world.book.fail = None
    world.book.calls.clear()
    plan = load_tick_plan(world.state, SETTINGS)
    assert world.live not in {b.portfolio_id for b in plan.books}
    world.tick(DAY3)
    assert world.book.calls == []


def test_a_short_broker_outage_skips_the_day_without_pausing(world):
    """Roadmap 19.5: an unreachable broker is a short outage. The book skips
    and the next day decides afresh. Only a long outage or a fault pauses."""
    from stonks.execution.brokers.base import BrokerUnavailableError

    world.book.fail = BrokerUnavailableError("gateway down")
    result = world.tick(DAY1)

    [live] = [r for r in result.portfolios if r.portfolio_id == world.live]
    assert live.status == "error" and "auto_paused" not in live.summary
    assert live.summary["broker_outage"] is True
    assert world.subs.get(world.bob, world.bob_auto).paused_reason is None

    world.book.fail = None
    result = world.tick(DAY2)
    [live] = [r for r in result.portfolios if r.portfolio_id == world.live]
    assert live.status == "ok"


def test_an_order_error_at_the_broker_pauses_auto(world):
    world.book.fail_orders = ProviderError("order endpoint failed", status=500)
    result = world.tick(DAY1)
    assert result.status == "partial"
    assert world.subs.get(world.bob, world.bob_auto).paused_reason is not None


def test_a_plain_rejection_does_not_pause_auto(world):
    world.book.quotes.pop("UP.US")  # the fake broker rejects orders it can't price
    world.tick(DAY1)
    [order] = world.orders(world.live)
    assert order["status"] == "rejected"
    assert world.subs.get(world.bob, world.bob_auto).paused_reason is None
    [run] = list_runs(world.state, world.live)
    assert run.orders_rejected == 1


def test_auto_books_without_a_trader_factory_are_skipped(world):
    plan = load_tick_plan(world.state, SETTINGS)
    run_tick(world.state, world.lake, world.registry, SETTINGS, as_of=DAY1, plan=plan)
    assert world.orders(world.live) == [] and world.book.orders == {}


def test_the_kill_switch_stops_a_user_and_a_portfolio_with_its_paper_account(world):
    trip_halt(world.state, "kill", reason="stop", actor="t", scope="user",
              user_id=world.alice.user_id, halt="all")  # fmt: skip
    trip_halt(world.state, "kill", reason="stop", actor="t", portfolio_id=world.live, halt="all")
    result = world.tick(DAY1)
    assert world.orders(world.sim) == []
    assert world.orders(world.live) == [] and world.book.orders == {}
    assert world.orders(paper_account_id(world.live)) == []
    halted = {r.portfolio_id: r.summary.get("halted") for r in result.portfolios}
    assert all(h and h["halt"] == "all" for h in halted.values())
    [run] = list_runs(world.state, world.sim)
    assert run.halted == "all" and run.risk_breached is False


def test_a_global_kill_switch_stops_every_book(world):
    trip_halt(world.state, "kill", reason="stop", actor="t", scope="global", halt="all")
    world.tick(DAY1)
    assert world.state.count_rows("orders") == 0


def test_a_breaker_halt_marks_the_run_and_restarts_the_paper_count(world):
    world.tick(DAY1)
    assert world.subs.get(world.alice, world.alice_paper).paper_days_completed == 1
    trip_halt(world.state, "drawdown", reason="deep", actor="system", portfolio_id=world.sim)
    world.tick(DAY2)
    [latest, _] = list_runs(world.state, world.sim)
    assert latest.risk_breached is True and latest.halted == "buys"
    assert world.subs.get(world.alice, world.alice_paper).paper_days_completed == 0


def test_owner_risk_limits_tighten_every_book_they_own(world):
    world.users.set_risk_policy(world.alice.user_id, {"max_weight_per_ticker": 0.05}, actor="t")
    plan = load_tick_plan(world.state, SETTINGS)
    by_id = {b.portfolio_id: b for b in plan.books}
    assert by_id[world.sim].spec.risk.max_weight_per_ticker == 0.05
    assert by_id[paper_account_id(world.live)].spec.risk.max_weight_per_ticker != 0.05
    world.tick(DAY1)
    [order] = world.orders(world.sim)
    qty = world.state.sql("SELECT quantity FROM orders WHERE client_id = ?", [order["client_id"]])
    assert qty[0][0] * 150.0 <= 10_000.0 * 0.05 + 1e-6


def test_a_dry_run_writes_no_runs_and_pauses_nothing(world):
    world.book.fail = ProviderError("down", status=503)
    world.tick(DAY1, dry_run=True)
    assert world.state.count_rows("portfolio_runs") == 0
    assert world.subs.get(world.bob, world.bob_auto).paused_reason is None


# ---- BE-01: auto only while the strategy is active --------------------------------------


def test_be01_a_demotion_pauses_auto_and_the_tick_places_nothing_at_the_broker(world):
    world.registry.set_status("bh_up", "shadow", actor="service:system", reason="quit rule")
    auto = world.subs.get(world.bob, world.bob_auto)
    assert auto.mode is Mode.AUTO
    assert auto.paused_reason == "strategy_not_active: shadow"
    [audit] = world.state.sql("SELECT * FROM audit_log WHERE action = 'subscription.auto_paused'")
    assert audit["target_id"] == world.bob_auto and audit["actor"] == "service:system"
    [notice] = world.state.sql("SELECT * FROM notification_outbox WHERE category = 'risk'")
    assert notice["user_id"] == world.bob.user_id

    world.tick(DAY1)
    assert world.book.orders == {}
    assert world.orders(world.live) == []
    # a paper subscription on a shadow strategy keeps trading
    [alice_order] = world.orders(world.sim)
    assert alice_order["strategy_id"] == "bh_up"


def test_be01_the_tick_refuses_and_pauses_an_auto_subscription_on_an_inactive_strategy(world):
    # e.g. a strategy demoted before the pause existed: its auto row still runs
    world.registry.set_status("bh_up", "retired", actor="t", reason="gone for good")
    world.state.execute("UPDATE subscriptions SET paused_reason = NULL WHERE id = ?",
                        [world.bob_auto])  # fmt: skip
    result = world.tick(DAY1)
    assert world.book.orders == {}
    assert world.orders(world.live) == []
    assert world.subs.get(world.bob, world.bob_auto).paused_reason == (
        "strategy_not_active: retired"
    )
    [live] = [r for r in result.portfolios if r.portfolio_id == world.live]
    assert live.summary.get("auto_paused") == [world.bob_auto]


def test_be01_a_dry_run_pauses_nothing_for_an_inactive_strategy(world):
    world.registry.set_status("bh_up", "shadow", actor="t", reason="quit rule")
    world.state.execute("UPDATE subscriptions SET paused_reason = NULL WHERE id = ?",
                        [world.bob_auto])  # fmt: skip
    world.tick(DAY1, dry_run=True)
    assert world.book.orders == {}
    assert world.subs.get(world.bob, world.bob_auto).paused_reason is None


# ---- BE-02: an auto book trades only what it owns ----------------------------------------


def _momentum_auto(world):
    from stonks.strategies.examples.momentum import Momentum

    reports = [SurvivalReport(test_id="oos", passed=True, metrics={})]
    params = {"lookback_days": 5, "skip_days": 0, "threshold": 0.0, "allocation": 0.5}
    world.registry.register(Momentum(params), reports=reports, strategy_id="mom")
    seed_status(world.registry, "mom", "active")
    world.state.execute("UPDATE subscriptions SET strategy_id = 'mom' WHERE id = ?",
                        [world.bob_auto])  # fmt: skip


def test_be02_an_auto_book_never_sells_the_users_own_holdings(world):
    from stonks.connections.base import ExternalPosition

    _momentum_auto(world)
    [account] = world.book.accounts
    world.book.positions = {
        account.id: [
            ExternalPosition(raw_symbol="FLAT", ticker="FLAT.US", quantity=10, price=50.0),
            ExternalPosition(raw_symbol="UP", ticker="UP.US", quantity=5, price=150.0),
        ]
    }
    world.tick(DAY1)
    placed = [(o.ticker, o.side) for o in world.book.orders.values()]
    assert ("FLAT.US", "sell") not in placed
    # momentum picks UP: the book buys its own UP even though Bob holds some
    assert placed == [("UP.US", "buy")]
    [bought] = world.book.orders.values()

    world.tick(DAY2)  # the book holds its UP now: nothing new, still no FLAT sell
    assert [(o.ticker, o.side) for o in world.book.orders.values()] == placed
    held = {p.ticker: p.quantity for p in world.book.positions[account.id]}
    assert held == {"FLAT.US": 10, "UP.US": 5 + bought.quantity}
    # the snapshot still marks the whole account
    [snap] = world.state.sql(
        "SELECT positions_json FROM portfolio_snapshots WHERE portfolio_id = ?"
        " ORDER BY as_of DESC LIMIT 1",
        [world.live],
    )
    assert "FLAT.US" in snap["positions_json"]


# ---- BE-03: a scoped tick trades only its scope ---------------------------------------------


@pytest.mark.parametrize("own_universe", [False, True])
def test_be03_a_scoped_tick_never_orders_outside_its_scope(world, own_universe):
    import json
    from dataclasses import replace

    from stonks.strategies.examples.momentum import Momentum

    reports = [SurvivalReport(test_id="oos", passed=True, metrics={})]
    params = {"lookback_days": 5, "skip_days": 0, "threshold": 0.0, "allocation": 0.5}
    world.registry.register(Momentum(params), reports=reports, strategy_id="mom")
    seed_status(world.registry, "mom", "active")
    world.state.execute("UPDATE subscriptions SET strategy_id = 'mom' WHERE id = ?",
                        [world.alice_paper])  # fmt: skip
    if own_universe:
        world.state.execute("UPDATE portfolios SET universe = ? WHERE id = ?",
                            [json.dumps(UNIVERSE), world.sim])  # fmt: skip
    world.tick(DAY1)
    [bought] = world.orders(world.sim)
    assert bought["ticker"] == "UP.US"

    scoped = replace(SETTINGS, universe=["DOWN.US"], scoped=True)
    plan = load_tick_plan(world.state, scoped, traders=world.traders)
    run_tick(world.state, world.lake, world.registry, scoped, as_of=DAY2, plan=plan)
    day2 = [o for o in world.orders(world.sim) if o["client_id"].startswith("2026-03-18")]
    assert all(o["ticker"] == "DOWN.US" for o in day2), day2


def test_be52_a_dry_run_plan_writes_no_paper_account(world):
    plan = load_tick_plan(world.state, SETTINGS, traders=world.traders, dry_run=True)
    assert paper_account_id(world.live) in {b.portfolio_id for b in plan.books}
    assert not world.state.sql("SELECT 1 FROM portfolios WHERE paper_of IS NOT NULL")
    run_tick(world.state, world.lake, world.registry, SETTINGS, as_of=DAY1, plan=plan,
             dry_run=True)  # fmt: skip
    assert not world.state.sql("SELECT 1 FROM portfolios WHERE paper_of IS NOT NULL")


# ---- BE-18: a retired strategy's holdings are exited, then its subscription ends -----------


def test_be18_a_retired_strategys_paper_holdings_are_sold_then_the_subscription_ends(world):
    # Alice's book also follows bh_down, which keeps its own holding
    world.subs.subscribe(world.alice, strategy_id="bh_down", mode=Mode.PAPER,
                         portfolio_id=world.sim)  # fmt: skip
    world.tick(DAY1)
    bought = {o["ticker"] for o in world.orders(world.sim)}
    assert "UP.US" in bought
    world.registry.set_status("bh_up", "retired", actor="t", reason="no edge left")

    world.tick(DAY2)
    day2 = [o for o in world.orders(world.sim) if o["client_id"].startswith("2026-03-18")]
    sells = [(o["ticker"], o["side"], o["strategy_id"]) for o in day2 if o["side"] == "sell"]
    assert sells == [("UP.US", "sell", "bh_up")]
    assert world.subs.get(world.alice, world.alice_paper).enabled is False
    [audit] = world.state.sql(
        "SELECT * FROM audit_log WHERE action = 'subscription.disable' AND target_id = ?",
        [world.alice_paper],
    )
    assert audit["actor"] == "service:system"
    [snap] = world.state.sql(
        "SELECT positions_json FROM portfolio_snapshots WHERE portfolio_id = ?"
        " ORDER BY as_of DESC, id DESC LIMIT 1",
        [world.sim],
    )
    assert "UP.US" not in snap["positions_json"]


# ---- BE-27: no future ticks ---------------------------------------------------------------


def test_be27_a_future_tick_is_refused_unless_it_is_a_dry_run(world):
    from stonks.production.tick import BackdatedTickError, FutureTickError

    future = date(2099, 1, 5)
    with pytest.raises(FutureTickError):
        world.tick(future)
    assert issubclass(FutureTickError, BackdatedTickError)
    assert world.state.count_rows("tick_runs") == 0
    world.tick(future, dry_run=True)  # a dry run may look ahead
    world.tick(DAY1)  # and real ticks still run
    assert world.orders(world.sim)


# ---- review 2026-09-27: approve mode trades live, so every pause covers it --------------


def _approve(world) -> None:
    world.state.execute("UPDATE subscriptions SET mode = 'approve' WHERE id = ?", [world.bob_auto])


def test_a_demotion_pauses_an_approve_subscription(world):
    _approve(world)
    world.registry.set_status("bh_up", "shadow", actor="service:system", reason="quit rule")
    sub = world.subs.get(world.bob, world.bob_auto)
    assert sub.mode is Mode.APPROVE and sub.paused_reason == "strategy_not_active: shadow"
    assert sub.auto_paused


def test_a_broker_error_pauses_an_approve_subscription(world):
    _approve(world)
    world.book.fail = ProviderError("fake broker is down", status=503)
    world.tick(DAY1)
    sub = world.subs.get(world.bob, world.bob_auto)
    assert sub.paused_reason is not None and sub.paused_reason.startswith("broker_error: ")


def test_disabling_a_user_pauses_their_approve_subscriptions(world):
    _approve(world)
    world.users.set_status(world.bob.user_id, "disabled", actor="t")
    [row] = world.state.sql(
        "SELECT paused_reason FROM subscriptions WHERE id = ?", [world.bob_auto]
    )
    assert row["paused_reason"] == "user_disabled"


def test_a_real_money_connection_below_live_small_opens_nothing(world):
    """Roadmap 19.9 outside IBKR: a linked account that trades real money
    sends no opening order while its portfolio stands below live_small.
    The guard sits on the trader the connection opens, so the tick, manual
    orders and tickets all meet it."""
    from stonks.production.live.stages import change_stage

    world.book.real_money = True
    world.tick(DAY1)
    assert world.book.orders == {}, "no opening order reached the real-money account"
    for stage in ("broker_paper", "live_small"):
        change_stage(world.state, world.live, stage, actor="t", reason="gates passed",
                     gate_report={"target": stage, "passed": True})  # fmt: skip
    world.tick(DAY2)
    assert world.book.orders, "at live_small the book opens"


def test_a_rejection_alert_names_holdings_only_to_the_owner(world):
    """Admins never see holdings: the operator alert of rejected orders
    counts them, and the owner's own alert names the tickers."""
    import json

    from tests.integration.test_production_tick_notify import Recorder

    world.book.quotes.pop("UP.US")  # the fake broker rejects an order without a quote
    recorder = Recorder()
    plan = load_tick_plan(world.state, SETTINGS, traders=world.traders)
    run_tick(world.state, world.lake, world.registry, SETTINGS, as_of=DAY1, plan=plan,
             notifier=recorder)  # fmt: skip
    [alert] = [n for n in recorder.sent if n.title == "orders rejected"]
    shown = alert.message + json.dumps(alert.fields, default=str)
    assert "UP.US" not in shown and world.live not in shown
    assert alert.fields["rejected"] == 1
    owner = world.state.sql(
        "SELECT message FROM alerts WHERE user_id = ? AND title LIKE '%rejected%'",
        [world.bob.user_id],
    )
    assert owner and "UP.US" in owner[0]["message"]
