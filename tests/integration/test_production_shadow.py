"""Shadow mode (roadmap 2.4): shadow strategies are ranked and evaluated
against their own virtual portfolios every tick, never traded."""

from __future__ import annotations

import json
from datetime import date

import pytest

import stonks.production.tick as tick_mod
from stonks.core.protocols import SurvivalReport
from stonks.production.tick import TickSettings, run_tick
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold

AS_OF = date(2026, 3, 20)
UNIVERSE = ["UP.US", "FLAT.US", "DOWN.US"]
SETTINGS = TickSettings(universe=UNIVERSE, initial_cash=10_000.0)


class ExplodingDecide(BuyAndHold):
    """Module-scope so the registry can re-import it by class path."""

    def decide(self, my_picks, portfolio, prices, as_of):
        raise RuntimeError("shadow decide blew up")


@pytest.fixture
def env(tmp_path, lake_trending):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
    yield lake_trending, state, registry
    state.close()


def _register(registry, strategy, status):
    sid = registry.register(
        strategy, reports=[SurvivalReport(test_id="oos", passed=True, metrics={})]
    )
    registry.set_status(sid, status)
    return sid


def _summary(state, tick_id):
    row = state.sql("SELECT summary_json FROM tick_runs WHERE id = ?", [tick_id])[0]
    return json.loads(row["summary_json"])


def test_migration_creates_shadow_tables(env):
    _, state, _ = env
    tables = set(state.tables())
    assert {"shadow_decisions", "shadow_portfolio_snapshots"} <= tables
    assert 2 in state.applied_migrations()


def test_shadow_strategy_is_evaluated_but_never_traded(env):
    lake, state, registry = env
    active = _register(registry, BuyAndHold({"ticker": "UP.US", "allocation": 1.0}), "active")
    shadow = _register(registry, BuyAndHold({"ticker": "FLAT.US", "allocation": 0.5}), "shadow")

    result = run_tick(state, lake, registry, SETTINGS, as_of=AS_OF)

    # real ledger only has the active strategy
    orders = state.sql("SELECT strategy_id, ticker FROM orders")
    assert [(r["strategy_id"], r["ticker"]) for r in orders] == [(active, "UP.US")]
    real_snap = state.sql("SELECT positions_json FROM portfolio_snapshots")
    assert set(json.loads(real_snap[0]["positions_json"])) == {"UP.US"}

    decisions = state.sql("SELECT * FROM shadow_decisions")
    assert len(decisions) == 1
    d = decisions[0]
    assert d["strategy_id"] == shadow
    assert d["tick_id"] == result.tick_id
    assert d["as_of"] == AS_OF.isoformat()
    assert (d["ticker"], d["side"], d["status"]) == ("FLAT.US", "buy", "filled")
    assert d["quantity"] == pytest.approx(100.0)  # 5000 / 50
    assert d["price"] == pytest.approx(50.0)

    snaps = state.sql("SELECT * FROM shadow_portfolio_snapshots")
    assert len(snaps) == 1
    assert snaps[0]["strategy_id"] == shadow
    assert snaps[0]["cash"] == pytest.approx(5_000.0)
    assert json.loads(snaps[0]["positions_json"]) == {"FLAT.US": pytest.approx(100.0)}
    assert snaps[0]["total_value"] == pytest.approx(10_000.0)

    [outcome] = _summary(state, result.tick_id)["shadow"]
    assert outcome["strategy_id"] == shadow
    assert outcome["status"] == "evaluated"
    assert outcome["decisions"] == 1


def test_shadow_runs_even_when_real_tick_is_noop(env):
    lake, state, registry = env
    shadow = _register(registry, BuyAndHold({"ticker": "FLAT.US", "allocation": 1.0}), "shadow")
    result = run_tick(state, lake, registry, SETTINGS, as_of=AS_OF)
    assert result.status == "noop"
    assert state.count_rows("orders") == 0
    assert state.count_rows("portfolio_snapshots") == 0
    assert state.sql("SELECT strategy_id FROM shadow_decisions")[0]["strategy_id"] == shadow


def test_shadow_rerun_same_day_is_idempotent(env):
    lake, state, registry = env
    _register(registry, BuyAndHold({"ticker": "FLAT.US", "allocation": 1.0}), "shadow")
    run_tick(state, lake, registry, SETTINGS, as_of=AS_OF)
    r2 = run_tick(state, lake, registry, SETTINGS, as_of=AS_OF)
    assert state.count_rows("shadow_decisions") == 1
    assert state.count_rows("shadow_portfolio_snapshots") == 1
    assert _summary(state, r2.tick_id)["shadow"][0]["status"] == "already_evaluated"


def test_shadow_virtual_portfolio_persists_between_ticks(env):
    lake, state, registry = env
    _register(registry, BuyAndHold({"ticker": "UP.US", "allocation": 1.0}), "shadow")
    run_tick(state, lake, registry, SETTINGS, as_of=AS_OF)
    run_tick(state, lake, registry, SETTINGS, as_of=date(2026, 3, 23))

    # BuyAndHold holds on day two: no new decision, but a new marked snapshot
    assert state.count_rows("shadow_decisions") == 1
    snaps = state.sql(
        "SELECT as_of, cash, positions_json, total_value FROM shadow_portfolio_snapshots "
        "ORDER BY as_of"
    )
    assert [s["as_of"] for s in snaps] == ["2026-03-20", "2026-03-23"]
    assert snaps[1]["cash"] == pytest.approx(snaps[0]["cash"])
    assert snaps[1]["positions_json"] == snaps[0]["positions_json"]
    assert snaps[1]["total_value"] > snaps[0]["total_value"]  # UP.US trends up


def test_shadow_does_not_backfill_before_a_later_snapshot(env):
    lake, state, registry = env
    _register(registry, BuyAndHold({"ticker": "UP.US", "allocation": 1.0}), "shadow")
    run_tick(state, lake, registry, SETTINGS, as_of=date(2026, 3, 23))
    r2 = run_tick(state, lake, registry, SETTINGS, as_of=AS_OF)
    assert state.count_rows("shadow_portfolio_snapshots") == 1
    assert _summary(state, r2.tick_id)["shadow"][0]["status"] == "out_of_order"


def test_failing_shadow_strategy_does_not_fail_tick_or_other_shadows(env):
    lake, state, registry = env
    _register(registry, BuyAndHold({"ticker": "UP.US", "allocation": 1.0}), "active")
    bad = _register(registry, ExplodingDecide({"ticker": "FLAT.US", "allocation": 1.0}), "shadow")
    good = _register(registry, BuyAndHold({"ticker": "DOWN.US", "allocation": 1.0}), "shadow")

    result = run_tick(state, lake, registry, SETTINGS, as_of=AS_OF)

    assert result.status == "ok"
    assert result.fills == 1
    outcomes = {o["strategy_id"]: o for o in _summary(state, result.tick_id)["shadow"]}
    assert outcomes[bad]["status"] == "failed"
    assert "blew up" in outcomes[bad]["error"]
    assert outcomes[good]["status"] == "evaluated"
    assert {r["strategy_id"] for r in state.sql("SELECT strategy_id FROM shadow_decisions")} == {
        good
    }
    assert (
        state.sql(
            "SELECT COUNT(*) AS n FROM shadow_portfolio_snapshots WHERE strategy_id = ?", [bad]
        )[0]["n"]
        == 0
    )


def test_unloadable_shadow_strategy_is_reported_failed_not_evaluated(env):
    lake, state, registry = env
    sid = _register(registry, BuyAndHold({"ticker": "FLAT.US", "allocation": 1.0}), "shadow")
    state.execute("UPDATE strategies SET class_path = 'stonks.gone:Gone' WHERE id = ?", [sid])
    result = run_tick(state, lake, registry, SETTINGS, as_of=AS_OF)
    [outcome] = _summary(state, result.tick_id)["shadow"]
    assert outcome["status"] == "failed"
    assert state.count_rows("shadow_portfolio_snapshots") == 0


def test_shadow_phase_crash_does_not_fail_real_tick(env, monkeypatch):
    lake, state, registry = env
    _register(registry, BuyAndHold({"ticker": "UP.US", "allocation": 1.0}), "active")
    _register(registry, BuyAndHold({"ticker": "FLAT.US", "allocation": 1.0}), "shadow")

    def boom(*a, **k):
        raise RuntimeError("shadow phase exploded")

    monkeypatch.setattr(tick_mod, "evaluate_shadow_strategies", boom)
    result = run_tick(state, lake, registry, SETTINGS, as_of=AS_OF)
    assert result.status == "ok"
    assert result.fills == 1
    assert state.count_rows("portfolio_snapshots") == 1
    assert "shadow phase exploded" in _summary(state, result.tick_id)["shadow_error"]


def test_shadow_applies_the_risk_policy(env):
    from stonks.production.risk import RiskPolicy

    lake, state, registry = env
    _register(registry, BuyAndHold({"ticker": "FLAT.US", "allocation": 1.0}), "shadow")
    settings = TickSettings(
        universe=UNIVERSE, initial_cash=10_000.0, risk=RiskPolicy(max_weight_per_ticker=0.2)
    )
    run_tick(state, lake, registry, settings, as_of=AS_OF)
    d = state.sql("SELECT quantity, price FROM shadow_decisions")[0]
    assert d["quantity"] * d["price"] == pytest.approx(2_000.0)


def test_dry_run_writes_no_shadow_rows(env):
    lake, state, registry = env
    _register(registry, BuyAndHold({"ticker": "FLAT.US", "allocation": 1.0}), "shadow")
    run_tick(state, lake, registry, SETTINGS, as_of=AS_OF, dry_run=True)
    assert state.count_rows("shadow_decisions") == 0
    assert state.count_rows("shadow_portfolio_snapshots") == 0


def test_shadow_disabled_writes_nothing(env):
    lake, state, registry = env
    _register(registry, BuyAndHold({"ticker": "FLAT.US", "allocation": 1.0}), "shadow")
    settings = TickSettings(universe=UNIVERSE, initial_cash=10_000.0, shadow_enabled=False)
    run_tick(state, lake, registry, settings, as_of=AS_OF)
    assert state.count_rows("shadow_decisions") == 0
