"""Daily gate metrics and gate reports of the live stages (roadmap 19.9)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from stonks.core.clock import FixedClock
from stonks.production.live.gates import (
    GateDay,
    GateFacts,
    ReconcileReportDrift,
    compute_gate_day,
    gate_days,
    gate_report,
    live_portfolios,
    record_gate_day,
    record_gate_days,
    stage_metrics,
)
from stonks.production.live.settings import StageGateSettings
from stonks.production.live.stages import change_stage

PF = "pf_default"
DAY = date(2026, 9, 28)
CLOCK = FixedClock(datetime(2026, 9, 28, 22, 0, tzinfo=UTC))
#: Stages move long before the sessions the tests record.
PROMOTED = FixedClock(datetime(2026, 8, 3, 12, 0, tzinfo=UTC))
NO_DRIFT = type("NoDrift", (), {"unexplained": lambda self, s, p, d: 0})()
NO_MODEL = type("NoModel", (), {"session_return": lambda self, s, p, d: None})()


def _order(state, cid, *, status="filled", state_=None, day=DAY, **kw):
    cols = {
        "client_id": cid,
        "ticker": "AAPL.US",
        "side": "buy",
        "quantity": 10.0,
        "order_type": "limit",
        "status": status,
        "state": state_ or status,
        "portfolio_id": PF,
        "created_at": f"{day.isoformat()}T13:30:00+00:00",
        "updated_at": f"{day.isoformat()}T13:31:00+00:00",
        **kw,
    }
    state.execute(
        f"INSERT INTO orders ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
        list(cols.values()),
    )


def _fill(state, cid, *, exec_id="e1", fee_currency="USD", price=100.0, day=DAY):
    state.execute(
        "INSERT INTO fills (order_client_id, ticker, quantity, price, fee, filled_at,"
        " portfolio_id, broker_exec_id, fee_currency) VALUES (?, 'AAPL.US', 10, ?, 1, ?, ?, ?, ?)",
        [cid, price, f"{day.isoformat()}T13:30:05+00:00", PF, exec_id, fee_currency],
    )


def _snapshot(state, day, value, pf=PF):
    state.execute(
        "INSERT INTO portfolio_snapshots (taken_at, cash, positions_json, total_value, as_of,"
        " portfolio_id) VALUES (?, 0, '{}', ?, ?, ?)",
        [f"{day.isoformat()}T21:00:00+00:00", value, day.isoformat(), pf],
    )


def _promote(state, *targets):
    for t in targets:
        change_stage(
            state, PF, t, actor="u", reason="r", gate_report={"target": t, "passed": True},
            clock=PROMOTED,
        )  # fmt: skip


def test_a_session_counts_orders_fills_and_stuck_orders(state):
    _order(state, "o1")
    _fill(state, "o1")
    _order(state, "o2", status="rejected")
    _order(state, "o3", status="pending", state_="submitted")
    _order(state, "o4")
    _fill(state, "o4", exec_id="e2", fee_currency=None)
    _order(state, "old", day=DAY - timedelta(days=1))
    gate = compute_gate_day(state, PF, DAY, drift=NO_DRIFT, model=NO_MODEL, clock=CLOCK)
    assert (gate.orders_sent, gate.orders_filled, gate.orders_rejected, gate.stuck_orders) == (
        4,
        2,
        1,
        1,
    )
    assert (gate.fills, gate.fills_missing_commission) == (2, 1)
    assert not gate.clean
    assert set(gate.dirty_reasons(StageGateSettings())) == {
        "1 stuck order(s)",
        "1 fill(s) with no commission",
        "rejection rate 25.0%",
    }


def test_a_quiet_session_is_clean(state):
    gate = compute_gate_day(state, PF, DAY, drift=NO_DRIFT, model=NO_MODEL)
    assert gate.clean and gate.orders_sent == 0 and gate.reject_rate == 0.0


def test_book_and_model_returns_give_the_tracking_difference(state):
    _snapshot(state, DAY - timedelta(days=1), 1000.0)
    _snapshot(state, DAY, 1010.0)
    model = type("M", (), {"session_return": lambda self, s, p, d: 0.004})()
    gate = compute_gate_day(state, PF, DAY, drift=NO_DRIFT, model=model)
    assert gate.live_return == pytest.approx(0.01)
    assert gate.tracking_diff == pytest.approx(0.006)


def test_the_shadow_model_book_weights_its_subscriptions(state):
    from stonks.production.live.gates import ShadowModelBook

    for sid in ("s_a", "s_b"):
        state.execute(
            "INSERT INTO strategies (id, class_path, params_json, status, created_at, updated_at)"
            " VALUES (?, 'x.Y', '{}', 'active', 'x', 'x')",
            [sid],
        )
    state.execute("INSERT INTO tick_runs (id, started_at, status) VALUES ('t0', 'x', 'ok')")
    for sid, before, after, weight in (("s_a", 100, 110, 1.0), ("s_b", 100, 100, 3.0)):
        state.execute(
            "INSERT INTO subscriptions (id, user_id, strategy_id, portfolio_id, mode, weight,"
            " created_at, updated_at) VALUES (?, 'usr_owner', ?, ?, 'auto', ?, 'x', 'x')",
            [f"sub_{sid}", sid, PF, weight],
        )
        for day, value in ((DAY - timedelta(days=1), before), (DAY, after)):
            state.execute(
                "INSERT INTO shadow_portfolio_snapshots (tick_id, strategy_id, as_of, taken_at,"
                " cash, positions_json, total_value) VALUES ('t0', ?, ?, 'x', 0, '{}', ?)",
                [sid, day.isoformat(), value],
            )
    assert ShadowModelBook().session_return(state, PF, DAY) == pytest.approx(0.1 / 4)
    assert ShadowModelBook().session_return(state, PF, DAY + timedelta(days=1)) is None


def test_drift_reads_the_sessions_end_of_day_report(state):
    assert ReconcileReportDrift().unexplained(state, PF, DAY) is None
    state.execute(
        "INSERT INTO reconcile_reports (id, portfolio_id, kind, as_of, taken_at, status,"
        " items_json) VALUES ('rec_a', ?, 'sod', ?, ?, 'drift', ?),"
        " ('rec_b', ?, 'eod', ?, ?, 'clean', '[]')",
        [PF, DAY.isoformat(), f"{DAY}T12:00:00+00:00", json.dumps([{"kind": "cash"}]),
         PF, DAY.isoformat(), f"{DAY}T20:15:00+00:00"],
    )  # fmt: skip
    # the end-of-day report wins over the start-of-day one
    assert ReconcileReportDrift().unexplained(state, PF, DAY) == 0


def test_only_live_stages_record_gate_days(state):
    assert live_portfolios(state) == []
    _promote(state, "broker_paper")
    assert live_portfolios(state) == [PF]


def _clean_days(state, n, stage, start=DAY):
    for i in range(n):
        record_gate_day(
            state,
            GateDay(
                portfolio_id=PF,
                session_date=start - timedelta(days=n - 1 - i),
                stage=stage,
                orders_sent=2,
                orders_filled=2,
                drift_items=0,
                clean=True,
                computed_at="x",
            ),
        )


def test_record_gate_days_alerts_a_dirty_week_and_never_changes_the_stage(state):
    _promote(state, "broker_paper")
    _clean_days(state, 4, "broker_paper", start=DAY - timedelta(days=1))
    _order(state, "stuck", status="pending", state_="submitted")
    alerts = []
    run = record_gate_days(
        state, DAY, drift=NO_DRIFT, model=NO_MODEL, alert=lambda pid, week: alerts.append(pid)
    )
    assert [g.portfolio_id for g in run.recorded] == [PF]
    assert run.dirty_weeks == (PF,) and alerts == [PF]
    assert len(gate_days(state, PF)) == 5
    from stonks.production.live.stages import get_stage

    assert get_stage(state, PF) == "broker_paper"
    # a rerun replaces the day
    record_gate_days(state, DAY, drift=NO_DRIFT, model=NO_MODEL, alert=lambda *a: None)
    assert len(gate_days(state, PF)) == 5


def test_gate_one_needs_paper_days_and_a_broker(state):
    report = gate_report(state, PF, facts=GateFacts(broker_linked=False), clock=CLOCK)
    assert report.target == "broker_paper" and not report.passed
    failed = {c.name for c in report.checks if c.passed is False}
    assert {"broker_linked", "subscriptions", "paper_days"} <= failed


def test_gate_two_counts_clean_sessions_in_broker_paper(state):
    _promote(state, "broker_paper")
    settings = StageGateSettings(min_broker_paper_sessions=5, broker_paper_clean_sessions=5)
    facts = GateFacts(broker_linked=True, allocation_set=True, profile_set=True)
    _clean_days(state, 4, "broker_paper")
    early = gate_report(state, PF, facts=facts, settings=settings, clock=CLOCK)
    assert early.target == "live_small" and not early.passed
    _clean_days(state, 5, "broker_paper")
    report = gate_report(state, PF, facts=facts, settings=settings, clock=CLOCK)
    by = {c.name: c for c in report.checks}
    assert by["sessions"].passed and by["clean_sessions"].passed
    assert by["kill_switch_drill"].passed is None  # 19.11 not built: not blocking
    assert report.passed
    assert report.as_dict()["metrics"]["sessions"] == 5


def test_gate_two_needs_the_allocation_and_profile(state):
    _promote(state, "broker_paper")
    settings = StageGateSettings(min_broker_paper_sessions=0, broker_paper_clean_sessions=0)
    report = gate_report(state, PF, facts=GateFacts(broker_linked=True), settings=settings)
    failed = {c.name for c in report.checks if c.passed is False}
    assert failed == {"allocation_set", "account_profile_set"}


def test_gate_three_needs_fills_and_the_tca_interval(state):
    _promote(state, "broker_paper", "live_small")
    settings = StageGateSettings(min_live_small_sessions=3, live_small_clean_sessions=3,
                                 min_live_fills=6)  # fmt: skip
    _clean_days(state, 3, "live_small")
    report = gate_report(state, PF, settings=settings, clock=CLOCK)
    by = {c.name: c for c in report.checks}
    assert by["filled_orders"].passed and by["filled_orders"].value == 6
    assert by["tca_gap"].passed is None  # no orders with a cost estimate
    assert report.passed


def test_the_top_stage_has_no_gate(state):
    _promote(state, "broker_paper", "live_small", "live_scale")
    report = gate_report(state, PF)
    assert report.target is None and not report.passed


def test_stage_metrics_tracking_error(state):
    for i, (live, model) in enumerate(((0.01, 0.0), (-0.01, 0.0), (0.02, 0.0))):
        record_gate_day(
            state,
            GateDay(
                portfolio_id=PF,
                session_date=DAY + timedelta(days=i),
                stage="sim_paper",
                live_return=live,
                model_return=model,
                computed_at="x",
            ),
        )
    m = stage_metrics(state, PF, "sim_paper", StageGateSettings())
    assert m["tracking_sessions"] == 3
    assert m["tracking_error"] == pytest.approx(0.015275 * 252**0.5, rel=1e-3)


def test_a_cost_model_that_is_too_cheap_fails_gate_three(state):
    _promote(state, "broker_paper", "live_small")
    settings = StageGateSettings(min_live_small_sessions=1, live_small_clean_sessions=1,
                                 min_live_fills=0)  # fmt: skip
    for i, price in enumerate((100.2, 100.3, 100.25)):
        cid = f"tca{i}"
        _order(
            state, cid, decision_price=100.0, decided_at=f"{DAY.isoformat()}T20:00:00+00:00",
            expected_cost_bps=2.0,
        )  # fmt: skip
        _fill(state, cid, exec_id=f"x{i}", price=price)
    gate = compute_gate_day(state, PF, DAY, drift=NO_DRIFT, model=NO_MODEL)
    assert gate.tca_orders == 3 and gate.tca_gap_bps > 15
    record_gate_day(state, gate)
    by = {c.name: c for c in gate_report(state, PF, settings=settings).checks}
    assert by["tca_gap"].passed is False
    assert "too cheap" in by["tca_gap"].detail
