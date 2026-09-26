"""W3.1 rules in the production tick and the model books: with a rule that
needs history enabled, the tick builds a risk context per book, so the rule
runs (here: max_holding force-sells a position held too long)."""

from __future__ import annotations

import json
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
from stonks.config import RiskPolicy
from stonks.core.protocols import SurvivalReport
from stonks.production.tick import TickSettings, load_tick_plan, run_tick
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from tests.fixtures.governance import seed_status

DAY1, DAY2 = date(2026, 3, 16), date(2026, 3, 20)
UNIVERSE = ["UP.US", "FLAT.US", "DOWN.US"]


def _settings(max_bars: int | None) -> TickSettings:
    rules = {"max_holding": {"max_holding_bars": max_bars}} if max_bars else {}
    return TickSettings(universe=UNIVERSE, risk=RiskPolicy(rules=rules))


@pytest.fixture
def env(tmp_path, lake_trending):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
    reports = [SurvivalReport(test_id="oos", passed=True, metrics={})]
    registry.register(
        BuyAndHold({"ticker": "UP.US", "allocation": 0.4}), reports=reports, strategy_id="bh_up"
    )
    seed_status(registry, "bh_up", "active")
    registry.register(
        BuyAndHold({"ticker": "FLAT.US", "allocation": 0.5}), reports=reports, strategy_id="bh_flat"
    )
    yield lake_trending, state, registry
    state.close()


def _positions(state, portfolio_id="pf_default"):
    row = state.sql(
        "SELECT positions_json FROM portfolio_snapshots WHERE portfolio_id = ?"
        " ORDER BY as_of DESC, id DESC LIMIT 1",
        [portfolio_id],
    )[0]
    return {t: q for t, q in json.loads(row[0]).items() if q}


def test_max_holding_force_sells_the_default_book(env):
    lake, state, registry = env
    run_tick(state, lake, registry, _settings(None), as_of=DAY1)
    assert "UP.US" in _positions(state)

    result = run_tick(state, lake, registry, _settings(2), as_of=DAY2)

    assert result.status == "ok"
    sell = state.sql(
        "SELECT status, strategy_id FROM orders WHERE client_id = ?",
        ["2026-03-20:risk.max_holding:UP.US:sell"],
    )
    assert [tuple(r) for r in sell] == [("filled", None)]
    assert "UP.US" not in _positions(state)


def test_without_the_rule_nothing_is_force_sold(env):
    lake, state, registry = env
    run_tick(state, lake, registry, _settings(None), as_of=DAY1)
    run_tick(state, lake, registry, _settings(None), as_of=DAY2)
    assert "UP.US" in _positions(state)
    assert not state.sql("SELECT 1 FROM orders WHERE client_id LIKE '%risk.max_holding%'")


def test_forced_sells_of_another_portfolio_carry_its_id(env):
    lake, state, registry = env
    user = UserRepository(state).create(display_name="Bob", role=Role.TRADER, actor="t")
    scope = Scope.for_user(user)
    pid = PortfolioRepository(state).create(scope, name="Bob").id
    SubscriptionRepository(state).subscribe(
        scope, strategy_id="bh_up", mode=Mode.PAPER, portfolio_id=pid, weight=1.0
    )
    run_tick(state, lake, registry, _settings(None), as_of=DAY1,
             plan=load_tick_plan(state, _settings(None)))  # fmt: skip
    settings = _settings(2)
    run_tick(state, lake, registry, settings, as_of=DAY2, plan=load_tick_plan(state, settings))

    rows = state.sql("SELECT client_id, portfolio_id FROM orders WHERE strategy_id IS NULL")
    assert [tuple(r) for r in rows] == [(f"2026-03-20:{pid}:risk.max_holding:UP.US:sell", pid)]
    assert "UP.US" not in _positions(state, pid)


def test_model_books_run_the_rules_too(env):
    lake, state, registry = env
    run_tick(state, lake, registry, _settings(None), as_of=DAY1)
    run_tick(state, lake, registry, _settings(2), as_of=DAY2)
    rows = state.sql(
        "SELECT side, status FROM shadow_decisions WHERE strategy_id = 'bh_flat' AND as_of = ?",
        [DAY2.isoformat()],
    )
    assert [tuple(r) for r in rows] == [("sell", "filled")]
    latest = state.sql(
        "SELECT positions_json FROM shadow_portfolio_snapshots WHERE strategy_id = 'bh_flat'"
        " ORDER BY as_of DESC LIMIT 1"
    )[0][0]
    assert not {t: q for t, q in json.loads(latest).items() if q}
