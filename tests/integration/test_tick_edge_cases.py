"""Edge cases of the production tick from review 18.1 (trading-ops, "missing
edge-case tests"): reruns after a rejection, zero cash, retired owners and
the circuit breaker on a tick that trades nothing."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.execution.brokers.base import OrderRejectedError
from stonks.portfolio.settings import ConstructionSettings
from stonks.production.halts import list_halts
from stonks.production.tick import TickSettings, run_tick
from tests.fixtures.governance import seed_status
from tests.fixtures.risk_rules import policy
from tests.integration.test_production_tick_exits import (
    AS_OF,
    ExitWhenUnpicked,
    RankAndRotate,
    _hold,
    _latest_snapshot,
    _register,
    _summary,
    env,  # noqa: F401 - fixture
)


def test_a_rerun_after_a_rejected_sell_resubmits_it_once(env, monkeypatch):  # noqa: F811
    lake, state, registry = env
    sid = _register(registry, ExitWhenUnpicked({"ticker": "UP.US", "allocation": 1.0}))
    _hold(state, sid, {"DOWN.US": 10.0})
    real = SimulatedBroker.place_order
    calls = {"n": 0}

    def reject_first(self, order):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OrderRejectedError("halted by the venue")
        return real(self, order)

    monkeypatch.setattr(SimulatedBroker, "place_order", reject_first)
    settings = TickSettings(universe=["UP.US", "DOWN.US"], initial_cash=10_000.0)

    run_tick(state, lake, registry, settings, as_of=AS_OF)
    [first] = state.sql("SELECT client_id, status FROM orders WHERE side = 'sell'")
    assert first["status"] == "rejected"

    run_tick(state, lake, registry, settings, as_of=AS_OF)  # same-day rerun
    rows = state.sql("SELECT client_id, status FROM orders WHERE side = 'sell'")
    assert [(r["client_id"], r["status"]) for r in rows] == [(first["client_id"], "filled")]
    assert calls["n"] == 2
    assert state.count_rows("fills") == 1

    run_tick(state, lake, registry, settings, as_of=AS_OF)  # and again: nothing new
    assert calls["n"] == 2


def test_zero_cash_with_a_targets_constructor_sells_and_drops_buys_cleanly(env):  # noqa: F811
    lake, state, registry = env
    sid = _register(registry, RankAndRotate({"ticker": "UP.US", "allocation": 1.0}))
    _hold(state, sid, {"DOWN.US": 10.0}, cash=0.0)
    settings = TickSettings(
        universe=["UP.US", "DOWN.US"],
        initial_cash=10_000.0,
        construction=ConstructionSettings(method="equal_weight_top_n"),
    )

    result = run_tick(state, lake, registry, settings, as_of=AS_OF)

    assert result.status == "ok"
    snap = _latest_snapshot(state)
    assert snap["cash"] >= 0.0
    sells = state.sql("SELECT status FROM orders WHERE side = 'sell'")
    assert [r["status"] for r in sells] == ["filled"]
    positions = json.loads(snap["positions_json"])
    assert positions.get("DOWN.US", 0.0) == pytest.approx(0.0)


def test_every_owner_retired_keeps_the_positions_and_says_why(env):  # noqa: F811
    lake, state, registry = env
    sid = _register(registry, ExitWhenUnpicked({"ticker": "UP.US", "allocation": 1.0}))
    _hold(state, sid, {"DOWN.US": 10.0}, cash=5.0)
    seed_status(registry, sid, "retired")

    result = run_tick(state, lake, registry, TickSettings(universe=["UP.US"]), as_of=AS_OF)

    assert result.status == "noop"
    assert _summary(state, result.tick_id)["reason"] == "no_active_owner"
    assert json.loads(_latest_snapshot(state)["positions_json"]) == {"DOWN.US": 10.0}
    assert state.sql("SELECT COUNT(*) FROM orders WHERE side = 'sell'")[0][0] == 0


def test_the_breaker_trips_on_a_tick_that_trades_nothing(env):  # noqa: F811
    """A noop tick (nothing ranked, nothing held) still records a breaker
    trip, so the owner hears about it the day it happens."""
    lake, state, registry = env
    _register(registry, ExitWhenUnpicked({"ticker": "UP.US", "allocation": 1.0}))
    start = AS_OF - timedelta(days=5)
    for i, value in enumerate([10_000.0, 9_700.0, 9_300.0]):
        day = start + timedelta(days=i)
        state.execute(
            "INSERT INTO portfolio_snapshots (taken_at, cash, positions_json, total_value,"
            " as_of, portfolio_id) VALUES (?, ?, '{}', ?, ?, 'pf_default')",
            [f"{day.isoformat()}T21:00:00+00:00", value, value, day.isoformat()],
        )
    breaker = policy(circuit_breaker={"max_month_loss": 0.06})
    settings = TickSettings(universe=["UP.US"], initial_cash=10_000.0, risk=breaker)

    result = run_tick(state, lake, registry, settings, as_of=AS_OF)

    assert result.status == "noop"
    [halt] = list_halts(state, on=AS_OF)
    assert halt.kind == "month_loss" and halt.portfolio_id == "pf_default"
    assert _summary(state, result.tick_id)["halted"]["gate"] == "risk_halts"


def test_a_dry_run_noop_records_no_breaker_trip(env):  # noqa: F811
    lake, state, registry = env
    _register(registry, ExitWhenUnpicked({"ticker": "UP.US", "allocation": 1.0}))
    for i, value in enumerate([10_000.0, 9_000.0]):
        day = date(2026, 3, 16) + timedelta(days=i)
        state.execute(
            "INSERT INTO portfolio_snapshots (taken_at, cash, positions_json, total_value,"
            " as_of, portfolio_id) VALUES (?, ?, '{}', ?, ?, 'pf_default')",
            [f"{day.isoformat()}T21:00:00+00:00", value, value, day.isoformat()],
        )
    breaker = policy(circuit_breaker={"max_month_loss": 0.06})
    settings = TickSettings(universe=["UP.US"], initial_cash=10_000.0, risk=breaker)
    run_tick(state, lake, registry, settings, as_of=AS_OF, dry_run=True)
    assert list_halts(state, include_cleared=True) == []
