"""The ``risk_halts`` trade gate (BL-28, 12.6): open halts and the circuit
breaker stop a portfolio's new orders before they reach its broker."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from stonks.production.halts import active_halts, clear_halt, list_halts, trip_halt
from stonks.production.hooks import GateContext, registered_gates
from stonks.production.hooks.risk_halts import RiskHaltGate
from stonks.store.state import SqliteState
from tests.fixtures.risk_rules import policy

AS_OF = date(2025, 6, 10)
PF = "pf_default"
OWNER = "usr_owner"
BREAKER = policy(circuit_breaker={"max_month_loss": 0.06, "max_drawdown_halt": 0.20})


@pytest.fixture
def state(tmp_path):
    s = SqliteState(tmp_path / "state.sqlite")
    s.migrate()
    yield s
    s.close()


def _ctx(state, *, pf=PF, owner=OWNER, dry_run=False, pol=None, as_of=AS_OF) -> GateContext:
    return GateContext(
        state=state, as_of=as_of, portfolio_id=pf, owner_id=owner, dry_run=dry_run, policy=pol
    )


def _snapshots(state, start: date, values: list[float], pf: str = PF) -> None:
    for i, v in enumerate(values):
        day = start + timedelta(days=i)
        state.execute(
            "INSERT INTO portfolio_snapshots (taken_at, cash, positions_json, total_value,"
            " as_of, portfolio_id) VALUES (?, ?, ?, ?, ?, ?)",
            [f"{day.isoformat()}T21:00:00+00:00", v, json.dumps({}), v, day.isoformat(), pf],
        )


def test_the_gate_is_registered():
    assert "risk_halts" in [g.name for g in registered_gates()]


def test_no_halt_lets_the_book_trade(state):
    assert RiskHaltGate().check(_ctx(state)) is None


def test_a_global_kill_switch_stops_every_order(state):
    trip_halt(state, "kill", reason="stop", actor="user:usr_owner", scope="global", halt="all")
    verdict = RiskHaltGate().check(_ctx(state, pf="pf_other", owner="usr_x"))
    assert verdict is not None and verdict.halt == "all"
    assert "kill" in verdict.reason and "stop" in verdict.reason


def test_a_user_kill_switch_covers_only_that_users_books(state):
    trip_halt(
        state, "kill", reason="mine", actor="user:usr_a", scope="user", user_id="usr_a", halt="all"
    )
    assert RiskHaltGate().check(_ctx(state, pf="pf_a", owner="usr_a")).halt == "all"
    assert RiskHaltGate().check(_ctx(state, pf="pf_b", owner="usr_b")) is None


def test_a_flatten_kill_switch_lets_sells_through(state):
    trip_halt(state, "kill", reason="flatten", actor="user:usr_owner", portfolio_id=PF, halt="buys")
    assert RiskHaltGate().check(_ctx(state)).halt == "buys"


def test_all_beats_buys(state):
    trip_halt(state, "drawdown", reason="dd", actor="system", portfolio_id=PF)
    trip_halt(state, "kill", reason="k", actor="user:usr_owner", scope="global", halt="all")
    verdict = RiskHaltGate().check(_ctx(state))
    assert verdict.halt == "all" and "drawdown" in verdict.reason


def test_the_breaker_trips_persists_and_notifies(state):
    _snapshots(state, date(2025, 6, 2), [10_000.0, 9_700.0, 9_300.0])
    verdict = RiskHaltGate().check(_ctx(state, pol=BREAKER))
    assert verdict is not None and verdict.halt == "buys" and "month_loss" in verdict.reason
    [halt] = active_halts(state, AS_OF, portfolio_id=PF)
    assert (halt.kind, halt.expires_on, halt.tripped_by) == (
        "month_loss",
        date(2025, 7, 1),
        "system",
    )
    assert state.sql("SELECT COUNT(*) FROM notification_outbox")[0][0] == 1
    # the next tick sees the open halt and notifies nobody again
    RiskHaltGate().check(_ctx(state, pol=BREAKER))
    assert state.sql("SELECT COUNT(*) FROM notification_outbox")[0][0] == 1


def test_the_breaker_in_a_dry_run_halts_without_writing(state):
    _snapshots(state, date(2025, 6, 2), [10_000.0, 9_300.0])
    verdict = RiskHaltGate().check(_ctx(state, pol=BREAKER, dry_run=True))
    assert verdict is not None and verdict.halt == "buys"
    assert list_halts(state, include_cleared=True) == []


def test_without_a_policy_the_breaker_is_not_evaluated(state):
    _snapshots(state, date(2025, 6, 2), [10_000.0, 9_000.0])
    assert RiskHaltGate().check(_ctx(state)) is None


def test_the_latched_drawdown_stays_until_cleared_then_does_not_retrip(state):
    _snapshots(state, date(2025, 4, 1), [10_000.0, 7_900.0])
    _snapshots(state, date(2025, 6, 1), [8_000.0, 8_100.0])
    gate = RiskHaltGate()
    assert "drawdown" in gate.check(_ctx(state, pol=BREAKER)).reason
    later = date(2025, 7, 15)
    assert gate.check(_ctx(state, pol=BREAKER, as_of=later)) is not None  # latched
    [halt] = active_halts(state, later, portfolio_id=PF)
    clear_halt(state, halt.id, actor="user:usr_owner", reason="reviewed")
    assert gate.check(_ctx(state, pol=BREAKER, as_of=later)) is None


def test_without_the_table_the_gate_halts_nothing(tmp_path):
    s = SqliteState(tmp_path / "bare.sqlite")
    try:
        assert RiskHaltGate().check(_ctx(s)) is None  # no table: nothing to enforce
    finally:
        s.close()


def test_a_kill_switch_stops_the_tick(tmp_path, lake_trending):
    from stonks.core.protocols import SurvivalReport
    from stonks.production.tick import TickSettings, run_tick
    from stonks.registry.store import StrategyRegistry
    from stonks.strategies.examples.buy_and_hold import BuyAndHold
    from tests.fixtures.governance import seed_status

    s = SqliteState(tmp_path / "state.sqlite")
    s.migrate()
    try:
        registry = StrategyRegistry(state=s, artifacts_dir=tmp_path / "artifacts")
        registry.register(
            BuyAndHold({"ticker": "UP.US", "allocation": 0.4}),
            reports=[SurvivalReport(test_id="oos", passed=True, metrics={})],
            strategy_id="bh_up",
        )
        seed_status(registry, "bh_up", "active")
        trip_halt(s, "kill", reason="panic", actor="user:usr_owner", scope="global", halt="all")
        settings = TickSettings(universe=["UP.US"], initial_cash=10_000.0)
        result = run_tick(s, lake_trending, registry, settings, as_of=date(2026, 3, 20))
        assert result.orders_placed == 0 and s.count_rows("orders") == 0
        summary = json.loads(s.sql("SELECT summary_json FROM tick_runs")[0][0])
        assert summary["halted"]["gate"] == "risk_halts"
        assert summary["halted"]["halt"] == "all"
    finally:
        s.close()
