"""The tick as a loop over portfolio books (BL-12, W2.1; design section 5):
per-portfolio ledgers, idempotency, attribution, modes and hooks."""

from __future__ import annotations

import json
from datetime import date

import pytest

from stonks.accounts import (
    DEFAULT_PORTFOLIO_ID,
    Mode,
    NotFound,
    PortfolioRepository,
    Role,
    Scope,
    SubscriptionRepository,
    UserRepository,
    owned_portfolio,
)
from stonks.core.protocols import SurvivalReport
from stonks.production import hooks as hooks_mod
from stonks.production import tick as tick_mod
from stonks.production.hooks import GateVerdict, PostTickHook, TradeGate
from stonks.production.tick import TickSettings, load_tick_plan, run_tick
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from stonks.strategies.examples.momentum import Momentum
from tests.fixtures.governance import seed_status
from tests.integration.test_signal_phase import RemembersItsScores

AS_OF = date(2026, 3, 20)
UNIVERSE = ["UP.US", "FLAT.US", "DOWN.US"]
SETTINGS = TickSettings(universe=UNIVERSE, initial_cash=10_000.0)


@pytest.fixture
def env(tmp_path, lake_trending):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
    reports = [SurvivalReport(test_id="oos", passed=True, metrics={})]
    for sid, strategy, status in [
        ("mom", Momentum({"lookback_days": 5, "threshold": 0.0, "allocation": 0.5}), "active"),
        ("bh_up", BuyAndHold({"ticker": "UP.US", "allocation": 0.4}), "active"),
        ("bh_flat", BuyAndHold({"ticker": "FLAT.US", "allocation": 0.5}), "shadow"),
    ]:
        registry.register(strategy, reports=reports, strategy_id=sid)
        if status != "shadow":
            seed_status(registry, sid, status)
    yield lake_trending, state, registry
    state.close()


class People:
    def __init__(self, state: SqliteState) -> None:
        self.users = UserRepository(state)
        self.portfolios = PortfolioRepository(state)
        self.subs = SubscriptionRepository(state)

    def trader(self, name: str) -> Scope:
        return Scope.for_user(self.users.create(display_name=name, role=Role.TRADER, actor="t"))

    def book(self, scope: Scope, name: str, strategies: dict[str, float], **kw) -> str:
        pid = self.portfolios.create(scope, name=name, **kw).id
        for sid, weight in strategies.items():
            self.subs.subscribe(
                scope, strategy_id=sid, mode=Mode.PAPER, portfolio_id=pid, weight=weight
            )
        return pid


def _orders(state, portfolio_id=None):
    sql = "SELECT client_id, strategy_id, ticker, side, status, portfolio_id FROM orders"
    rows = state.sql(
        sql + (" WHERE portfolio_id = ?" if portfolio_id else ""),
        [portfolio_id] if portfolio_id else [],
    )
    return [dict(r) for r in rows]


def _summary(state, tick_id):
    return json.loads(state.sql("SELECT summary_json FROM tick_runs WHERE id = ?", [tick_id])[0][0])


def test_two_users_each_trade_and_see_only_their_own_book(env):
    lake, state, registry = env
    people = People(state)
    alice, bob = people.trader("Alice"), people.trader("Bob")
    pf_a = people.book(alice, "Alice", {"bh_up": 1.0})
    pf_b = people.book(bob, "Bob", {"bh_flat": 1.0})  # a shadow strategy, paper mode

    plan = load_tick_plan(state, SETTINGS)
    assert {b.portfolio_id for b in plan.books} == {pf_a, pf_b}  # pf_default has no subs
    result = run_tick(state, lake, registry, SETTINGS, as_of=AS_OF, plan=plan)

    assert result.status == "ok" and result.fills == 2
    a_orders, b_orders = _orders(state, pf_a), _orders(state, pf_b)
    assert [(o["strategy_id"], o["ticker"]) for o in a_orders] == [("bh_up", "UP.US")]
    assert [(o["strategy_id"], o["ticker"]) for o in b_orders] == [("bh_flat", "FLAT.US")]
    assert a_orders[0]["client_id"] == f"2026-03-20:{pf_a}:bh_up:UP.US:buy"
    assert len(_orders(state)) == 2

    # What each user can reach through the scoped repositories.
    mine = {p.id for p in people.portfolios.list(alice)}
    assert mine == {pf_a}
    with pytest.raises(NotFound):
        owned_portfolio(state, alice, pf_b)
    snaps = state.sql("SELECT portfolio_id, positions_json FROM portfolio_snapshots")
    positions = {r["portfolio_id"]: json.loads(r["positions_json"]) for r in snaps}
    assert set(positions[pf_a]) == {"UP.US"} and set(positions[pf_b]) == {"FLAT.US"}
    fills = {r["portfolio_id"] for r in state.sql("SELECT portfolio_id FROM fills")}
    assert fills == {pf_a, pf_b}

    summary = _summary(state, result.tick_id)
    assert set(summary["portfolios"]) == {pf_a, pf_b}
    assert summary["orders_placed"] == 2


def test_a_same_day_rerun_places_nothing_twice(env):
    lake, state, registry = env
    people = People(state)
    alice, bob = people.trader("Alice"), people.trader("Bob")
    people.book(alice, "Alice", {"bh_up": 1.0, "mom": 1.0})
    people.book(bob, "Bob", {"bh_up": 1.0})
    plan = load_tick_plan(state, SETTINGS)

    first = run_tick(state, lake, registry, SETTINGS, as_of=AS_OF, plan=plan)
    before = _orders(state)
    again = run_tick(state, lake, registry, SETTINGS, as_of=AS_OF, plan=plan)

    assert first.orders_placed == 2 and again.orders_placed == 0
    assert _orders(state) == before
    assert state.count_rows("fills") == 2


def test_rerun_across_the_accounts_upgrade_keeps_default_client_ids(
    tmp_path, lake_trending, monkeypatch
):
    """A tick run on the pre-accounts schema, then the migration, then a
    same-day re-run: the default portfolio's client ids are unchanged, so
    nothing is placed twice."""
    from tests.integration.accounts.legacy import legacy_state, migrate_to_current

    state = legacy_state(tmp_path / "state.sqlite", monkeypatch)
    registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
    sid = registry.register(
        BuyAndHold({"ticker": "UP.US", "allocation": 0.5}),
        reports=[SurvivalReport(test_id="oos", passed=True, metrics={})],
    )
    seed_status(registry, sid, "active")
    first = run_tick(state, lake_trending, registry, SETTINGS, as_of=AS_OF)
    migrate_to_current(state)
    again = run_tick(state, lake_trending, registry, SETTINGS, as_of=AS_OF)

    assert first.orders_placed == 1 and again.orders_placed == 0
    [order] = _orders(state)
    assert order["client_id"] == f"2026-03-20:{sid}:UP.US:buy"
    assert order["portfolio_id"] == DEFAULT_PORTFOLIO_ID
    state.close()


def test_one_strategy_in_two_books_decides_separately_for_each(env):
    lake, state, registry = env
    registry.register(
        RemembersItsScores({"ticker": "UP.US", "allocation": 0.5}),
        reports=[SurvivalReport(test_id="oos", passed=True, metrics={})],
        strategy_id="mem",
    )
    seed_status(registry, "mem", "active")
    people = People(state)
    alice, bob = people.trader("Alice"), people.trader("Bob")
    pf_a = people.book(alice, "Alice", {"mem": 1.0})
    pf_b = people.book(bob, "Bob", {"mem": 1.0}, initial_cash=2_000.0)

    result = run_tick(
        state, lake, registry, SETTINGS, as_of=AS_OF, plan=load_tick_plan(state, SETTINGS)
    )

    assert result.fills == 2  # both books kept the day's evaluation
    qty = {
        r["portfolio_id"]: r["quantity"]
        for r in state.sql("SELECT portfolio_id, quantity FROM orders")
    }
    assert qty[pf_a] == pytest.approx(5 * qty[pf_b])  # each sized off its own cash


def test_equal_weight_constructor_gives_every_strategy_capital(env):
    lake, state, registry = env
    people = People(state)
    alice = people.trader("Alice")
    pf = people.book(
        alice,
        "Combined",
        {"bh_up": 1.0, "bh_flat": 1.0},
        construction={"method": "equal_weight_top_n", "n": 2},
    )
    result = run_tick(
        state, lake, registry, SETTINGS, as_of=AS_OF, plan=load_tick_plan(state, SETTINGS)
    )

    orders = _orders(state, pf)
    assert {(o["strategy_id"], o["ticker"]) for o in orders} == {
        ("bh_up", "UP.US"),
        ("bh_flat", "FLAT.US"),
    }
    assert _summary(state, result.tick_id)["constructor"] == "equal_weight_top_n"
    rows = state.sql(
        "SELECT ticker, strategy_id, subscription_id, weight_share, target_weight, source"
        " FROM position_attribution WHERE portfolio_id = ? ORDER BY ticker",
        [pf],
    )
    subs = {s.strategy_id: s.id for s in people.subs.list_for_portfolio(alice, pf)}
    assert [(r["ticker"], r["strategy_id"], r["source"]) for r in rows] == [
        ("FLAT.US", "bh_flat", "target"),
        ("UP.US", "bh_up", "target"),
    ]
    assert all(r["subscription_id"] == subs[r["strategy_id"]] for r in rows)
    assert all(r["weight_share"] == pytest.approx(1.0) for r in rows)
    assert all(r["target_weight"] == pytest.approx(0.5) for r in rows)


def test_single_winner_attribution_is_recorded_and_carried(env):
    lake, state, registry = env
    run_tick(state, lake, registry, SETTINGS, as_of=date(2026, 3, 19))
    run_tick(state, lake, registry, SETTINGS, as_of=AS_OF)
    rows = state.sql(
        "SELECT as_of, ticker, strategy_id, weight_share, source FROM position_attribution"
        " WHERE portfolio_id = ? ORDER BY as_of, ticker",
        [DEFAULT_PORTFOLIO_ID],
    )
    assert [tuple(r) for r in rows] == [
        ("2026-03-19", "UP.US", "bh_up", 1.0, "decision"),
        ("2026-03-20", "UP.US", "bh_up", 1.0, "carried"),
    ]


def test_notify_subscriptions_place_nothing_and_are_handed_to_the_hooks(env, monkeypatch):
    lake, state, registry = env
    people = People(state)
    carol = people.trader("Carol")
    people.subs.subscribe(carol, strategy_id="bh_flat", mode=Mode.NOTIFY)
    seen = []

    class Capture(PostTickHook):
        name, stage = "capture", "tick"

        def run(self, ctx):
            seen.append(list(ctx.notify_signals))

    monkeypatch.setitem(hooks_mod._HOOKS, "capture", Capture)
    plan = load_tick_plan(state, SETTINGS)
    assert plan.books == ()
    result = run_tick(state, lake, registry, SETTINGS, as_of=AS_OF, plan=plan)

    assert result.orders_placed == 0 and state.count_rows("orders") == 0
    [[signal]] = seen
    assert (signal.strategy_id, signal.picks) == ("bh_flat", (("FLAT.US", 1.0),))


def test_auto_counts_only_on_broker_portfolios_and_paused_portfolios_wait(env):
    lake, state, registry = env
    people = People(state)
    alice = people.trader("Alice")
    pf = people.book(alice, "Alice", {"bh_up": 1.0})
    state.execute("UPDATE subscriptions SET mode = 'auto' WHERE portfolio_id = ?", [pf])
    assert load_tick_plan(state, SETTINGS).books == ()

    state.execute("UPDATE subscriptions SET mode = 'paper' WHERE portfolio_id = ?", [pf])
    people.portfolios.set_status(alice, pf, "paused")
    assert load_tick_plan(state, SETTINGS).books == ()


def test_one_failing_book_does_not_stop_the_others(env, monkeypatch):
    lake, state, registry = env
    people = People(state)
    pf_a = people.book(people.trader("Alice"), "Alice", {"bh_up": 1.0})
    pf_b = people.book(people.trader("Bob"), "Bob", {"bh_up": 1.0})
    real = tick_mod._load_or_seed_portfolio

    def flaky(state, initial_cash, portfolio_id=None):
        if portfolio_id == pf_b:
            raise RuntimeError("bob's book is corrupt")
        return real(state, initial_cash, portfolio_id)

    monkeypatch.setattr(tick_mod, "_load_or_seed_portfolio", flaky)
    result = run_tick(
        state, lake, registry, SETTINGS, as_of=AS_OF, plan=load_tick_plan(state, SETTINGS)
    )

    assert result.status == "partial"
    assert [o["portfolio_id"] for o in _orders(state)] == [pf_a]
    books = _summary(state, result.tick_id)["portfolios"]
    assert books[pf_b]["status"] == "error" and "corrupt" in books[pf_b]["error"]


# ---- hooks and gates -------------------------------------------------------------


def test_a_tick_hook_runs_once_per_tick_and_a_failing_one_is_reported(env, monkeypatch):
    lake, state, registry = env
    calls = []

    class Once(PostTickHook):
        name, stage = "once", "tick"

        def run(self, ctx):
            calls.append(ctx.tick_id)
            return {"once": True}

    class Broken(PostTickHook):
        name, stage = "broken", "tick"

        def run(self, ctx):
            raise RuntimeError("hook exploded")

    monkeypatch.setitem(hooks_mod._HOOKS, "once", Once)
    monkeypatch.setitem(hooks_mod._HOOKS, "broken", Broken)
    result = run_tick(state, lake, registry, SETTINGS, as_of=AS_OF)

    assert calls == [result.tick_id]
    summary = _summary(state, result.tick_id)
    assert summary["once"] is True
    assert "hook exploded" in summary["hook_errors"]["broken"]
    assert result.status == "ok" and result.fills == 1


def test_a_failing_portfolio_hook_rolls_back_alone(env, monkeypatch):
    lake, state, registry = env

    class Half(PostTickHook):
        name, stage = "half", "portfolio"

        def run(self, ctx):
            ctx.state.execute("DELETE FROM portfolio_snapshots")
            raise RuntimeError("half-written")

    monkeypatch.setitem(hooks_mod._HOOKS, "half", Half)
    result = run_tick(state, lake, registry, SETTINGS, as_of=AS_OF)

    assert state.count_rows("portfolio_snapshots") == 1  # the delete was rolled back
    assert state.count_rows("fills") == 1
    assert "half-written" in _summary(state, result.tick_id)["hook_errors"]["half"]


def test_a_gate_halting_buys_lets_sells_through(env, monkeypatch):
    lake, state, registry = env
    seed_status(registry, "bh_up", "retired")
    run_tick(state, lake, registry, SETTINGS, as_of=date(2026, 3, 19))  # mom buys UP.US
    # Nothing ranks above 2.0, so mom exits (a sell) and nothing buys.
    exits = TickSettings(universe=UNIVERSE, initial_cash=10_000.0, threshold=2.0)

    class KillBuys(TradeGate):
        name = "kill_buys"

        def check(self, ctx):
            return GateVerdict(halt="buys", reason="kill switch")

    monkeypatch.setitem(hooks_mod._GATES, "kill_buys", KillBuys)
    result = run_tick(state, lake, registry, exits, as_of=AS_OF)

    summary = _summary(state, result.tick_id)
    assert summary["halted"] == {"halt": "buys", "gate": "kill_buys", "reason": "kill switch"}
    todays = state.sql("SELECT ticker, side FROM orders WHERE tick_id = ?", [result.tick_id])
    assert [tuple(r) for r in todays] == [("UP.US", "sell")]


def test_a_gate_halting_everything_places_nothing(env, monkeypatch):
    lake, state, registry = env

    class KillAll(TradeGate):
        name = "kill_all"

        def check(self, ctx):
            return GateVerdict(halt="all", reason="portfolio paused by owner")

    monkeypatch.setitem(hooks_mod._GATES, "kill_all", KillAll)
    result = run_tick(state, lake, registry, SETTINGS, as_of=AS_OF)
    assert result.orders_placed == 0 and state.count_rows("orders") == 0
    assert state.count_rows("portfolio_snapshots") == 1  # still marked


def test_paper_subscriptions_never_reach_a_live_broker(env):
    """On an install whose default portfolio trades at a real broker, only
    auto subscriptions place orders there; paper ones stay out of it."""
    _, state, _ = env
    owner = Scope.for_user(UserRepository(state).get("usr_owner"))
    subs = SubscriptionRepository(state)
    subs.subscribe(owner, strategy_id="bh_up", mode=Mode.PAPER, portfolio_id=DEFAULT_PORTFOLIO_ID)
    subs.subscribe(owner, strategy_id="mom", mode=Mode.PAPER, portfolio_id=DEFAULT_PORTFOLIO_ID)
    state.execute("UPDATE subscriptions SET mode = 'auto' WHERE strategy_id = 'mom'")
    live = TickSettings(universe=UNIVERSE, broker_kind="alpaca")

    [book] = load_tick_plan(state, live).books
    assert tick_mod.book_strategies(book, ["bh_up", "mom"], live) == ["mom"]
    [book] = load_tick_plan(state, SETTINGS).books
    # simulated: paper trades, and auto has no broker to reach (left out)
    assert tick_mod.book_strategies(book, ["bh_up", "mom"], SETTINGS) == ["bh_up"]
