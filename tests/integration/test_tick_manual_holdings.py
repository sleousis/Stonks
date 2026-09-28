"""Roadmap 20.1: the tick never trades what a person bought by hand.

A simulated book holds a manual position next to its strategy's. The
strategy's target book leaves the ticker out, which would sell a holding
the book owns, but the manual one is kept and the snapshot still shows it."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from stonks.accounts import (
    Mode,
    PortfolioRepository,
    Role,
    Scope,
    SubscriptionRepository,
    UserRepository,
)
from stonks.config import RiskPolicy
from stonks.core.protocols import SurvivalReport
from stonks.production.manual import ManualBook, ManualOrder, place_manual_order
from stonks.production.tick import TickSettings, load_tick_plan, run_tick
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from tests.fixtures.governance import seed_status

AS_OF = date(2026, 3, 20)
SETTINGS = TickSettings(universe=["UP.US", "FLAT.US", "DOWN.US"], initial_cash=10_000.0)


@pytest.fixture
def env(tmp_path, lake_trending):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
    registry.register(
        BuyAndHold({"ticker": "UP.US", "allocation": 0.5}),
        reports=[SurvivalReport(test_id="oos", passed=True, metrics={})],
        strategy_id="bh_up",
    )
    seed_status(registry, "bh_up", "active")
    user = UserRepository(state).create(display_name="alice", role=Role.TRADER, actor="t")
    scope = Scope.for_user(user)
    pid = (
        PortfolioRepository(state)
        .create(
            scope,
            name="Alice",
            initial_cash=10_000.0,
            construction={"method": "equal_weight_top_n"},
        )
        .id
    )
    SubscriptionRepository(state).subscribe(
        scope, strategy_id="bh_up", mode=Mode.PAPER, portfolio_id=pid, weight=1.0
    )
    yield lake_trending, state, registry, pid, user.id
    state.close()


def _buy_flat(state, lake, pid, owner) -> str:
    out = place_manual_order(
        state,
        lake,
        ManualOrder(
            portfolio_id=pid,
            ticker="FLAT.US",
            side="buy",
            quantity=20.0,
            reason="my own pick",
            actor=f"user:{owner}",
        ),
        ManualBook(portfolio_id=pid, owner_id=owner, risk=RiskPolicy(), initial_cash=10_000.0),
        SETTINGS,
        now=datetime(2026, 3, 19, 15, 0, tzinfo=UTC),
    )
    assert out.status == "filled"
    return out.client_id


def _sells(state, pid):
    return [
        r["ticker"]
        for r in state.sql(
            "SELECT ticker FROM orders WHERE portfolio_id = ? AND side = 'sell'", [pid]
        )
    ]


def test_the_tick_keeps_a_manual_holding(env):
    lake, state, registry, pid, owner = env
    _buy_flat(state, lake, pid, owner)
    result = run_tick(
        state, lake, registry, SETTINGS, as_of=AS_OF, plan=load_tick_plan(state, SETTINGS)
    )
    assert result.status == "ok"
    assert "FLAT.US" not in _sells(state, pid)
    snap = state.sql(
        "SELECT positions_json FROM portfolio_snapshots WHERE portfolio_id = ?"
        " ORDER BY id DESC LIMIT 1",
        [pid],
    )[0]
    positions = json.loads(snap["positions_json"])
    assert positions["FLAT.US"] == pytest.approx(20.0)
    assert positions.get("UP.US", 0.0) > 0


def test_the_same_holding_bought_by_a_strategy_is_sold(env):
    """Control: the holding is only safe because it is manual."""
    lake, state, registry, pid, owner = env
    cid = _buy_flat(state, lake, pid, owner)
    state.execute("UPDATE orders SET origin = 'strategy' WHERE client_id = ?", [cid])
    run_tick(state, lake, registry, SETTINGS, as_of=AS_OF, plan=load_tick_plan(state, SETTINGS))
    assert "FLAT.US" in _sells(state, pid)


def test_a_manual_holding_is_no_drawdown_for_the_strategies(tmp_path, lake_trending):
    """The equity curve records the whole account, manual holding included.
    The drawdown rules must compare it with the whole account too, not with
    the book's own part, or a person's manual buy reads as a loss and stops
    every strategy buy."""
    from stonks.production.rules.settings import RuleSettings

    risk = RiskPolicy(
        rules=RuleSettings.model_validate(
            {
                "circuit_breaker": {"max_drawdown_halt": 0.2},
                "drawdown_scaling": {"schedule": ((0.1, 0.5), (0.3, 0.0))},
            }
        )
    )
    settings = TickSettings(universe=SETTINGS.universe, initial_cash=10_000.0, risk=risk)
    state = SqliteState(tmp_path / "dd_state.sqlite")
    state.migrate()
    try:
        registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "dd_artifacts")
        registry.register(
            BuyAndHold({"ticker": "UP.US", "allocation": 0.3}),
            reports=[SurvivalReport(test_id="oos", passed=True, metrics={})],
            strategy_id="bh_up",
        )
        seed_status(registry, "bh_up", "active")
        user = UserRepository(state).create(display_name="bob", role=Role.TRADER, actor="t")
        scope = Scope.for_user(user)
        pid = PortfolioRepository(state).create(scope, name="Bob", initial_cash=10_000.0).id
        SubscriptionRepository(state).subscribe(
            scope, strategy_id="bh_up", mode=Mode.PAPER, portfolio_id=pid, weight=1.0
        )
        out = place_manual_order(
            state,
            lake_trending,
            ManualOrder(
                portfolio_id=pid,
                ticker="FLAT.US",
                side="buy",
                quantity=100.0,
                reason="half the book, my own pick",
                actor=f"user:{user.id}",
            ),
            ManualBook(portfolio_id=pid, owner_id=user.id, risk=risk, initial_cash=10_000.0),
            settings,
            now=datetime(2026, 3, 19, 15, 0, tzinfo=UTC),
        )
        assert out.status == "filled"
        result = run_tick(
            state,
            lake_trending,
            registry,
            settings,
            as_of=AS_OF,
            plan=load_tick_plan(state, settings),
        )
        [book] = result.portfolios
        rules = {a["rule"] for a in book.summary.get("risk_adjustments", [])}
        assert not rules & {"circuit_breaker", "drawdown_scaling"}, book.summary
        bought = state.sql(
            "SELECT quantity FROM orders WHERE portfolio_id = ? AND ticker = 'UP.US'", [pid]
        )
        assert bought and bought[0]["quantity"] > 0
    finally:
        state.close()


def test_a_manual_holding_is_not_attributed_to_a_strategy(env):
    """Per-strategy P&L (the quit rule, the risk monitor) reads the
    attributed quantity. A person's manual shares of the same ticker are
    theirs, never the strategy's."""
    lake, state, registry, pid, owner = env
    out = place_manual_order(
        state,
        lake,
        ManualOrder(
            portfolio_id=pid,
            ticker="UP.US",
            side="buy",
            quantity=7.0,
            reason="my own shares",
            actor=f"user:{owner}",
        ),
        ManualBook(portfolio_id=pid, owner_id=owner, risk=RiskPolicy(), initial_cash=10_000.0),
        SETTINGS,
        now=datetime(2026, 3, 19, 15, 0, tzinfo=UTC),
    )
    assert out.status == "filled"
    run_tick(state, lake, registry, SETTINGS, as_of=AS_OF, plan=load_tick_plan(state, SETTINGS))
    bought = sum(
        r["quantity"]
        for r in state.sql(
            "SELECT f.quantity FROM fills f JOIN orders o ON o.client_id = f.order_client_id"
            " WHERE o.portfolio_id = ? AND o.origin = 'strategy' AND o.ticker = 'UP.US'",
            [pid],
        )
    )
    assert bought > 0
    [row] = state.sql(
        "SELECT quantity FROM position_attribution WHERE portfolio_id = ? AND ticker = 'UP.US'",
        [pid],
    )
    assert row["quantity"] == pytest.approx(bought)
