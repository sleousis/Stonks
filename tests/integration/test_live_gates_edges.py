"""Edges of the live stage gates (roadmap 19.9): state DBs from before the
tables existed, unreadable rows, the drills table of 19.11, the tracking
error limit, refused orders read from the tick summaries, and one
portfolio failing without stopping the others."""

from __future__ import annotations

import json
from datetime import UTC, datetime, time, timedelta

import pytest

import tests.integration.test_live_gates as base
from stonks.production.drills import DrillReport, DrillStep, record_drill
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


def _drill(state, *, age, passed=True, portfolio_id=PF):
    report = DrillReport(
        broker="simulated",
        portfolio_id="pf_drill",
        client_id="drill-1",
        started_at=CLOCK.now(),
        finished_at=CLOCK.now(),
        steps=(DrillStep("engage_kill", passed, "engaged", 1.0),),
    )
    when = datetime.combine(DAY - timedelta(days=age), time(10), tzinfo=UTC)
    record_drill(state, report, ran_by="cli:ops", portfolio_id=portfolio_id, ran_at=when)


def test_a_state_db_before_the_drills_table_blocks_the_gate(state):
    state.execute("DROP TABLE kill_switch_drills")
    check = _live_small_checks(state, StageGateSettings())["kill_switch_drill"]
    assert check.passed is False and "stonks db init" in check.detail


def test_no_drill_blocks_the_gate_and_says_how_to_run_one(state):
    check = _live_small_checks(state, StageGateSettings())["kill_switch_drill"]
    assert check.passed is False
    assert "no passing kill switch drill" in check.detail
    assert "stonks halts drill" in check.detail


def test_a_failed_drill_does_not_count(state):
    _drill(state, age=1, passed=False)
    check = _live_small_checks(state, StageGateSettings())["kill_switch_drill"]
    assert check.passed is False and "last kill switch drill failed" in check.detail


def test_a_failed_drill_after_a_pass_blocks_the_gate(state):
    _drill(state, age=9, passed=True)
    _drill(state, age=1, passed=False)
    check = _live_small_checks(state, StageGateSettings())["kill_switch_drill"]
    assert check.passed is False
    assert "last kill switch drill failed" in check.detail
    assert "stonks halts drill" in check.detail


def test_a_pass_after_a_failed_drill_clears_the_gate(state):
    _drill(state, age=5, passed=False)
    _drill(state, age=1, passed=True)
    check = _live_small_checks(state, StageGateSettings())["kill_switch_drill"]
    assert check.passed is True


def test_a_drill_of_another_portfolio_does_not_count(state):
    _drill(state, age=1, portfolio_id="pf_other")
    check = _live_small_checks(state, StageGateSettings())["kill_switch_drill"]
    assert check.passed is False


def test_a_global_drill_counts_for_every_portfolio(state):
    _drill(state, age=1, portfolio_id=None)
    check = _live_small_checks(state, StageGateSettings())["kill_switch_drill"]
    assert check.passed is True


@pytest.mark.parametrize(("age", "passed"), [(3, True), (30, True), (31, False)])
def test_a_passing_drill_counts_for_thirty_days(state, age, passed):
    _drill(state, age=age)
    check = _live_small_checks(state, StageGateSettings())["kill_switch_drill"]
    assert (check.passed, check.value, check.required) == (passed, age, 30)
    if not passed:
        assert "older than 30 days" in check.detail and "stonks halts drill" in check.detail


@pytest.mark.parametrize(("age", "passed"), [(7, True), (8, False)])
def test_the_drill_window_comes_from_settings(state, age, passed):
    _drill(state, age=age)
    settings = StageGateSettings(drill_max_age_days=7)
    check = _live_small_checks(state, settings)["kill_switch_drill"]
    assert (check.passed, check.required) == (passed, 7)


def test_the_live_scale_gate_needs_a_recent_drill_too(state):
    base._promote(state, "broker_paper", "live_small")
    report = gate_report(state, PF, settings=StageGateSettings(), clock=CLOCK)
    assert report.target == "live_scale"
    by = {c.name: c for c in report.checks}
    assert by["kill_switch_drill"].passed is False
