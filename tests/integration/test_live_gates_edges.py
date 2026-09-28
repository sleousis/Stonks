"""Edges of the live stage gates (roadmap 19.9): state DBs from before the
tables existed, unreadable rows, the drills table of 19.11, the tracking
error limit, refused orders read from the tick summaries, and one
portfolio failing without stopping the others."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

import tests.integration.test_live_gates as base
from stonks.production.live.gates import (
    GateDay,
    ReconcileReportDrift,
    compute_gate_day,
    gate_days,
    gate_report,
    live_portfolios,
    owner_alert,
    record_gate_day,
    record_gate_days,
    stage_metrics,
)
from stonks.production.live.settings import StageGateSettings

PF, DAY, CLOCK = base.PF, base.DAY, base.CLOCK
NO_DRIFT, NO_MODEL = base.NO_DRIFT, base.NO_MODEL


def _report(state, items_json, rid="rec_a"):
    state.execute(
        "INSERT INTO reconcile_reports (id, portfolio_id, kind, as_of, taken_at, status,"
        " items_json) VALUES (?, ?, 'eod', ?, ?, 'clean', ?)",
        [rid, PF, DAY.isoformat(), f"{DAY}T20:15:00+00:00", items_json],
    )


# ---- drift -----------------------------------------------------------------------


def test_a_state_db_before_reconcile_reports_has_no_drift_to_read(state):
    state.execute("DROP TABLE reconcile_reports")
    assert ReconcileReportDrift().unexplained(state, PF, DAY) is None


@pytest.mark.parametrize("items", ["not json", '{"kind": "cash"}'])
def test_an_unreadable_report_counts_as_no_report(state, items):
    _report(state, items)
    assert ReconcileReportDrift().unexplained(state, PF, DAY) is None


def test_drift_makes_a_session_dirty(state):
    drift = type("D", (), {"unexplained": lambda self, s, p, d: 2})()
    gate = compute_gate_day(state, PF, DAY, drift=drift, model=NO_MODEL)
    assert not gate.clean and gate.drift_items == 2
    assert gate.dirty_reasons(StageGateSettings()) == ["2 unexplained drift item(s)"]


# ---- the tables ------------------------------------------------------------------


def test_a_state_db_before_the_stage_tables_has_no_gate_days(state):
    base._promote(state, "broker_paper")
    for table in ("live_gate_days", "live_stage_changes"):  # migration 037
        state.execute(f"DROP TABLE {table}")
    assert gate_days(state, PF) == [] and live_portfolios(state) == []


# ---- recording every portfolio ----------------------------------------------------


def test_one_portfolio_failing_never_stops_the_run(state):
    base._promote(state, "broker_paper")
    boom = type("D", (), {"unexplained": lambda self, s, p, d: 1 / 0})()
    run = record_gate_days(state, DAY, drift=boom, model=NO_MODEL, alert=lambda *a: None)
    assert run.recorded == () and run.dirty_weeks == ()
    assert gate_days(state, PF) == []


def test_the_default_alert_only_logs_a_dirty_week(state):
    base._promote(state, "broker_paper")
    base._clean_days(state, 4, "broker_paper", start=DAY - timedelta(days=1))
    base._order(state, "stuck", status="pending", state_="submitted")
    run = record_gate_days(state, DAY, drift=NO_DRIFT, model=NO_MODEL)
    assert run.dirty_weeks == (PF,)


def test_the_owner_alert_survives_a_router_failure(state, monkeypatch):
    import stonks.notify.router as router

    def broken(_state):
        raise RuntimeError("no channels")

    monkeypatch.setattr(router, "configured_router", broken)
    week = [GateDay(portfolio_id=PF, session_date=DAY, stage="broker_paper", clean=False)]
    owner_alert(state)(PF, week)  # logs, never raises


# ---- metrics ---------------------------------------------------------------------


def test_the_clean_streak_stops_at_the_last_dirty_session(state):
    for i, clean in enumerate((True, False, True, True)):
        record_gate_day(state, GateDay(portfolio_id=PF, session_date=DAY + timedelta(days=i),
                                       stage="sim_paper", clean=clean, computed_at="x"))  # fmt: skip
    m = stage_metrics(state, PF, "sim_paper", StageGateSettings())
    assert (m["sessions"], m["clean_sessions"], m["clean_streak"]) == (4, 3, 2)


def test_orders_without_a_cost_estimate_or_a_fill_leave_the_tca_gap_out(state):
    decided = f"{DAY.isoformat()}T20:00:00+00:00"
    base._order(state, "no_estimate", decision_price=100.0, decided_at=decided)
    base._fill(state, "no_estimate", exec_id="x1", price=100.2)
    base._order(state, "unfilled", status="pending", state_="accepted", decision_price=100.0,
                decided_at=decided, expected_cost_bps=2.0)  # fmt: skip
    gate = compute_gate_day(state, PF, DAY, drift=NO_DRIFT, model=NO_MODEL)
    assert gate.tca_orders == 0 and gate.tca_gap_bps is None


# ---- refused orders --------------------------------------------------------------


def _tick(state, tid, summary):
    state.execute(
        "INSERT INTO tick_runs (id, started_at, status, summary_json) VALUES (?, 'x', 'ok', ?)",
        [tid, summary],
    )


def test_refused_orders_come_from_the_sessions_real_ticks(state):
    d = DAY.isoformat()
    refused = {"adjusted_quantity": 0}
    cut = {"adjusted_quantity": 3}
    _tick(state, f"tick_{d}_a", json.dumps({"portfolios": {PF: {"risk_adjustments": [refused,
                                                                                   cut]}}}))  # fmt: skip
    _tick(state, f"tick_{d}_b", json.dumps({"portfolio_id": PF,
                                            "risk_adjustments": [refused, "junk"]}))  # fmt: skip
    _tick(state, f"tick_{d}_c", json.dumps({"dry_run": True, "portfolio_id": PF,
                                            "risk_adjustments": [refused]}))  # fmt: skip
    _tick(state, f"tick_{d}_d", "not json")
    _tick(state, f"tick_{d}_e", json.dumps({"portfolios": {"other": {"risk_adjustments":
                                                                     [refused]}}}))  # fmt: skip
    gate = compute_gate_day(state, PF, DAY, drift=NO_DRIFT, model=NO_MODEL)
    assert gate.orders_refused == 2
    assert gate.clean  # our own rules refusing orders is not a failure


# ---- gate checks -----------------------------------------------------------------


def _tracking_days(state):
    for i, live in enumerate((0.01, -0.01, 0.02)):
        record_gate_day(state, GateDay(portfolio_id=PF, session_date=DAY + timedelta(days=i),
                                       stage="broker_paper", live_return=live, model_return=0.0,
                                       drift_items=0, computed_at="x"))  # fmt: skip


def _live_small_checks(state, settings):
    base._promote(state, "broker_paper")
    _tracking_days(state)
    return {c.name: c for c in gate_report(state, PF, settings=settings, clock=CLOCK).checks}


def test_tracking_error_is_reported_only_without_a_limit(state):
    check = _live_small_checks(state, StageGateSettings())["tracking_error"]
    assert check.passed is None and check.detail.endswith("a year (reported only)")
    assert check.value == pytest.approx(0.2425, rel=1e-3)


@pytest.mark.parametrize(("limit", "passed"), [(0.5, True), (0.1, False)])
def test_tracking_error_against_its_limit(state, limit, passed):
    check = _live_small_checks(state, StageGateSettings(max_tracking_error=limit))
    assert check["tracking_error"].passed is passed
    assert check["tracking_error"].required == limit


def _drills_table(state, columns="portfolio_id TEXT, passed INTEGER, created_at TEXT"):
    state.execute(f"CREATE TABLE kill_switch_drills ({columns})")


def test_a_drills_table_of_another_shape_is_unavailable(state):
    _drills_table(state, "id TEXT")
    check = _live_small_checks(state, StageGateSettings())["kill_switch_drill"]
    assert check.passed is None and "unknown drills table" in check.detail


def test_no_passing_drill_fails_the_gate(state):
    _drills_table(state)
    state.execute("INSERT INTO kill_switch_drills VALUES (?, 0, ?)", [PF, DAY.isoformat()])
    check = _live_small_checks(state, StageGateSettings())["kill_switch_drill"]
    assert check.passed is False and check.detail == "no passing drill yet"


@pytest.mark.parametrize(("age", "passed"), [(3, True), (30, True), (31, False)])
def test_a_passing_drill_counts_for_thirty_days(state, age, passed):
    _drills_table(state)
    when = (DAY - timedelta(days=age)).isoformat() + "T10:00:00+00:00"
    state.execute("INSERT INTO kill_switch_drills VALUES (?, 1, ?)", [PF, when])
    check = _live_small_checks(state, StageGateSettings())["kill_switch_drill"]
    assert (check.passed, check.value) == (passed, age)
