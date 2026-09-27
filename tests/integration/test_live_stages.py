"""The live stage state machine and its audit trail (roadmap 19.9)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from stonks.core.clock import FixedClock
from stonks.production.live.stages import (
    STAGES,
    StageError,
    change_stage,
    get_stage,
    next_stage,
    stage_history,
    trades_real_money,
)

PF = "pf_default"
CLOCK = FixedClock(datetime(2026, 9, 28, 21, 0, tzinfo=UTC))


def _report(passed: bool = True, target: str = "broker_paper") -> dict:
    return {"target": target, "passed": passed, "checks": []}


def test_every_portfolio_starts_in_sim_paper(state):
    assert get_stage(state, PF) == "sim_paper"
    assert stage_history(state, PF) == []


def test_stage_order_and_real_money():
    assert STAGES == ("sim_paper", "broker_paper", "live_small", "live_scale")
    assert next_stage("sim_paper") == "broker_paper"
    assert next_stage("live_scale") is None
    assert [trades_real_money(s) for s in STAGES] == [False, False, True, True]


def test_a_promotion_moves_one_step_and_is_logged_with_its_report(state):
    change = change_stage(
        state, PF, "broker_paper", actor="user:usr_owner", reason="soak starts",
        gate_report=_report(), clock=CLOCK,
    )  # fmt: skip
    assert (change.from_stage, change.to_stage, change.direction) == (
        "sim_paper",
        "broker_paper",
        "promote",
    )
    assert get_stage(state, PF) == "broker_paper"
    [row] = stage_history(state, PF)
    assert row.gate_report == _report()
    assert row.actor == "user:usr_owner"
    audit = state.sql("SELECT action, details_json FROM audit_log WHERE action LIKE 'live.stage%'")
    assert audit[0]["action"] == "live.stage_promoted"
    assert json.loads(audit[0]["details_json"])["to_stage"] == "broker_paper"


def test_a_promotion_needs_a_passing_report(state):
    with pytest.raises(StageError, match="gate"):
        change_stage(state, PF, "broker_paper", actor="u", reason="r", gate_report=None)
    with pytest.raises(StageError, match="did not pass"):
        change_stage(state, PF, "broker_paper", actor="u", reason="r", gate_report=_report(False))
    with pytest.raises(StageError, match="another stage"):
        change_stage(
            state, PF, "broker_paper", actor="u", reason="r",
            gate_report=_report(target="live_small"),
        )  # fmt: skip
    assert get_stage(state, PF) == "sim_paper"


def test_a_promotion_never_skips_a_stage(state):
    with pytest.raises(StageError, match="one stage at a time"):
        change_stage(
            state, PF, "live_small", actor="u", reason="r",
            gate_report=_report(target="live_small"),
        )  # fmt: skip


def test_a_demotion_goes_down_any_number_of_steps_without_a_report(state):
    for target in ("broker_paper", "live_small"):
        change_stage(state, PF, target, actor="u", reason="up", gate_report=_report(target=target))
    change = change_stage(state, PF, "sim_paper", actor="u", reason="drift twice")
    assert (change.direction, change.gate_report) == ("demote", None)
    assert get_stage(state, PF) == "sim_paper"
    assert [c.to_stage for c in stage_history(state, PF)] == [
        "sim_paper",
        "live_small",
        "broker_paper",
    ]


@pytest.mark.parametrize(("target", "reason"), [("sim_paper", "r"), ("broker_paper", " ")])
def test_bad_requests_are_refused(state, target, reason):
    with pytest.raises(StageError):
        change_stage(state, PF, target, actor="u", reason=reason, gate_report=_report())


def test_unknown_portfolio_or_stage_is_refused(state):
    with pytest.raises(StageError, match="not found"):
        change_stage(state, "pf_nope", "broker_paper", actor="u", reason="r", gate_report={})
    with pytest.raises(StageError, match="unknown stage"):
        change_stage(state, PF, "live", actor="u", reason="r")  # type: ignore[arg-type]


def test_the_database_refuses_an_unaudited_stage_write(state):
    with pytest.raises(sqlite3.DatabaseError, match="live_stage_changes"):
        state.execute("UPDATE portfolios SET live_stage = 'live_small' WHERE id = ?", [PF])
    change_stage(state, PF, "broker_paper", actor="u", reason="r", gate_report=_report())
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        state.execute("DELETE FROM live_stage_changes")
