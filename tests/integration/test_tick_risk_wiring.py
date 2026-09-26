"""The tick wires the live circuit breaker (the book's policy reaches the
``risk_halts`` gate) and the quit rule (settings and registry reach the
tick hooks)."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from stonks.config import RiskPolicy
from stonks.core.protocols import SurvivalReport
from stonks.production import tick as tick_module
from stonks.production.halts import active_halts
from stonks.production.quit_rule import QuitRuleSettings
from stonks.production.tick import TickSettings, run_tick
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from tests.fixtures.governance import seed_status

AS_OF = date(2026, 3, 20)


@pytest.fixture
def tick_env(tmp_path, lake_trending):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
    seed_status(
        registry,
        registry.register(
            BuyAndHold({"ticker": "UP.US", "allocation": 1.0}),
            reports=[SurvivalReport(test_id="oos", passed=True, metrics={"sharpe_oos": 1.0})],
        ),
        "active",
    )
    yield lake_trending, state, registry
    state.close()


def _snapshots(state: SqliteState, start: date, values: list[float]) -> None:
    for i, v in enumerate(values):
        day = start + timedelta(days=i)
        state.execute(
            "INSERT INTO portfolio_snapshots (taken_at, cash, positions_json, total_value,"
            " as_of, portfolio_id) VALUES (?, ?, ?, ?, ?, ?)",
            [f"{day.isoformat()}T21:00:00+00:00", v, json.dumps({}), v, day.isoformat(),
             "pf_default"],
        )  # fmt: skip


def test_the_live_circuit_breaker_halts_buys_and_opens_a_halt(tick_env):
    lake, state, registry = tick_env
    _snapshots(state, date(2026, 3, 2), [10_000.0, 9_600.0, 9_000.0])
    policy = RiskPolicy(rules={"circuit_breaker": {"max_month_loss": 0.06}})
    settings = TickSettings(universe=["UP.US"], initial_cash=10_000.0, risk=policy)

    result = run_tick(state, lake, registry, settings, as_of=AS_OF)

    assert result.orders_placed == 0
    [halt] = active_halts(state, AS_OF, portfolio_id="pf_default")
    assert halt.kind == "month_loss"


def test_without_the_breaker_the_same_book_buys(tick_env):
    lake, state, registry = tick_env
    _snapshots(state, date(2026, 3, 2), [10_000.0, 9_600.0, 9_000.0])
    settings = TickSettings(universe=["UP.US"], initial_cash=10_000.0)

    result = run_tick(state, lake, registry, settings, as_of=AS_OF)

    assert result.orders_placed == 1
    assert active_halts(state, AS_OF, portfolio_id="pf_default") == []


def test_tick_hooks_get_the_settings_and_the_registry(tick_env, monkeypatch):
    lake, state, registry = tick_env
    seen = []
    real = tick_module.run_tick_hooks

    def spy(ctx, log=None):
        seen.append(ctx)
        return real(ctx, log) if log is not None else real(ctx)

    monkeypatch.setattr(tick_module, "run_tick_hooks", spy)
    quit_rule = QuitRuleSettings(quit_multiple=2.0, auto_demote=True)
    settings = TickSettings(universe=["UP.US"], initial_cash=10_000.0, quit_rule=quit_rule)

    run_tick(state, lake, registry, settings, as_of=AS_OF)

    [ctx] = seen
    assert ctx.settings.quit_rule == quit_rule
    assert ctx.registry is registry
