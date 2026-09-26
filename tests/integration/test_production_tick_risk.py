"""The risk layer sits between ``strategy.decide`` and the broker in run_tick."""

from __future__ import annotations

import json
from datetime import date

import pytest

from stonks.core.protocols import SurvivalReport
from stonks.production.risk import RiskPolicy
from stonks.production.tick import TickSettings, run_tick
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold

AS_OF = date(2026, 3, 20)


@pytest.fixture
def tick_env(tmp_path, lake_trending):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
    sid = registry.register(
        BuyAndHold({"ticker": "UP.US", "allocation": 1.0}),
        reports=[SurvivalReport(test_id="oos", passed=True, metrics={})],
    )
    registry.set_status(sid, "active")
    yield lake_trending, state, registry
    state.close()


def _summary(state, tick_id):
    row = state.sql("SELECT summary_json FROM tick_runs WHERE id = ?", [tick_id])[0]
    return json.loads(row["summary_json"])


def test_tick_clips_buy_to_max_weight_per_ticker(tick_env):
    lake, state, registry = tick_env
    settings = TickSettings(
        universe=["UP.US"],
        initial_cash=10_000.0,
        risk=RiskPolicy(max_weight_per_ticker=0.3),
    )
    result = run_tick(state, lake, registry, settings, as_of=AS_OF)

    assert result.fills == 1
    fill = state.sql("SELECT quantity, price FROM fills")[0]
    assert fill["quantity"] * fill["price"] == pytest.approx(3_000.0)
    snap = state.sql("SELECT cash FROM portfolio_snapshots")[0]
    assert snap["cash"] == pytest.approx(7_000.0)

    summary = _summary(state, result.tick_id)
    [adj] = summary["risk_adjustments"]
    assert adj["ticker"] == "UP.US"
    assert adj["rule"] == "max_weight_per_ticker"


def test_tick_where_risk_drops_every_order_places_nothing(tick_env):
    lake, state, registry = tick_env
    settings = TickSettings(
        universe=["UP.US"],
        initial_cash=10_000.0,
        risk=RiskPolicy(max_open_positions=0),
    )
    result = run_tick(state, lake, registry, settings, as_of=AS_OF)

    assert result.status == "ok"
    assert result.orders_placed == 0
    assert state.count_rows("orders") == 0
    # the snapshot still lands so the portfolio ledger stays continuous
    assert state.count_rows("portfolio_snapshots") == 1
    assert _summary(state, result.tick_id)["risk_adjustments"][0]["rule"] == "max_open_positions"


def test_dry_run_applies_risk_too(tick_env):
    lake, state, registry = tick_env
    settings = TickSettings(
        universe=["UP.US"],
        initial_cash=10_000.0,
        risk=RiskPolicy(max_open_positions=0),
    )
    result = run_tick(state, lake, registry, settings, as_of=AS_OF, dry_run=True)
    assert result.orders_placed == 0
    assert _summary(state, result.tick_id)["risk_adjustments"]


def test_tick_without_risk_limits_has_empty_adjustments(tick_env):
    lake, state, registry = tick_env
    settings = TickSettings(universe=["UP.US"], initial_cash=10_000.0)
    result = run_tick(state, lake, registry, settings, as_of=AS_OF)
    assert _summary(state, result.tick_id)["risk_adjustments"] == []
