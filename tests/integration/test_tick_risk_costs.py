"""The tick's risk cash buffer prices buys with the configured cost model
(roadmap 11.7), not the legacy slippage / fee (0 when the model is on)."""

from __future__ import annotations

import json
from datetime import date

import pytest

from stonks.backtest.costs import AssetClassCosts, CostModelSettings
from stonks.core.protocols import SurvivalReport
from stonks.production.risk import RiskPolicy
from stonks.production.tick import TickSettings, run_tick
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold

AS_OF = date(2026, 3, 20)
COSTS = CostModelSettings(default=AssetClassCosts(fee_bps=100.0))  # 1% of notional


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


def test_tick_cash_buffer_survives_cost_model_fees(tick_env):
    lake, state, registry = tick_env
    settings = TickSettings(
        universe=["UP.US"],
        initial_cash=10_000.0,
        costs=COSTS,
        risk=RiskPolicy(cash_buffer_fraction=0.1),
        shadow_enabled=False,
    )
    result = run_tick(state, lake, registry, settings, as_of=AS_OF)
    assert result.fills == 1
    snap = state.sql("SELECT cash FROM portfolio_snapshots")[0]
    # 10% of 10_000 stays in cash after the 1% fee.
    assert snap["cash"] == pytest.approx(1_000.0, abs=1e-6)
    summary = json.loads(
        state.sql("SELECT summary_json FROM tick_runs WHERE id = ?", [result.tick_id])[0][
            "summary_json"
        ]
    )
    assert [a["rule"] for a in summary["risk_adjustments"]] == ["cash_buffer"]


def test_shadow_cash_buffer_survives_cost_model_fees(tick_env, monkeypatch):
    import stonks.production.shadow as shadow

    lake, state, registry = tick_env
    seen: list[dict] = []
    real = shadow.apply_risk

    def spy(*args, **kwargs):
        seen.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(shadow, "apply_risk", spy)
    sid = registry.register(BuyAndHold({"ticker": "UP.US", "allocation": 1.0}), reports=[])
    settings = TickSettings(
        universe=["UP.US"],
        initial_cash=10_000.0,
        costs=COSTS,
        risk=RiskPolicy(cash_buffer_fraction=0.1),
    )
    run_tick(state, lake, registry, settings, as_of=AS_OF)
    assert seen, "shadow evaluation did not run"
    assert seen[0]["cost_model"] == COSTS
    assert sid
