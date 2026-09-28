"""The live options gate and the approval level (roadmap 17.8)."""

from __future__ import annotations

import json

import pytest

from stonks.options.live.approval import (
    ApprovalError,
    get_approval,
    guard_level,
    set_approval,
)
from stonks.options.live.gate import gate_lookup, options_live_state
from stonks.production.live.stages import change_stage


def promote_to(state, portfolio_id: str, stage: str) -> None:
    for to in ("broker_paper", "live_small", "live_scale"):
        change_stage(
            state,
            portfolio_id,
            to,  # type: ignore[arg-type]
            actor="user:usr_owner",
            reason="test",
            gate_report={"target": to, "passed": True},
        )
        if to == stage:
            return


def test_the_level_is_none_until_the_owner_sets_one(state):
    got = get_approval(state, "pf_default")
    assert got.level == "none" and got.reason is None


def test_setting_the_level_is_audited_with_the_old_one(state):
    set_approval(state, "pf_default", "covered", actor="user:usr_owner", reason="start")
    got = set_approval(state, "pf_default", "spreads", actor="user:usr_owner", reason="more")
    assert (got.level, got.updated_by, got.reason) == ("spreads", "user:usr_owner", "more")
    rows = state.sql(
        "SELECT details_json FROM audit_log WHERE action = 'options.approval_set' ORDER BY id"
    )
    assert [json.loads(r["details_json"])["previous"] for r in rows] == ["none", "covered"]


@pytest.mark.parametrize(("level", "reason"), [("level9", "x"), ("covered", " ")])
def test_bad_levels_and_missing_reasons_are_refused(state, level, reason):
    with pytest.raises(ApprovalError):
        set_approval(state, "pf_default", level, actor="user:u", reason=reason)
    assert get_approval(state, "pf_default").level == "none"


def test_levels_map_onto_the_guard_levels():
    assert [guard_level(x) for x in ("none", "covered", "spreads", "naked")] == [None, 2, 3, 4]


def test_the_gate_needs_the_switch_the_stage_and_a_level():
    assert options_live_state(True, "live_small", "covered").allowed
    off = options_live_state(False, "sim_paper", "none")
    assert not off.allowed and len(off.reasons) == 3
    assert "options live is off" in (off.refusal or "")
    assert not options_live_state(True, "broker_paper", "spreads").allowed
    assert not options_live_state(True, "live_scale", "none").allowed


def test_gate_lookup_reads_the_state_at_each_call(state):
    switch = {"on": False}
    gate = gate_lookup(state, "pf_default", live=lambda: switch["on"])
    assert gate().reasons and gate().level == "none"
    switch["on"] = True
    promote_to(state, "pf_default", "live_small")
    set_approval(state, "pf_default", "covered", actor="user:usr_owner", reason="go")
    now = gate()
    assert now.allowed and (now.stage, now.level) == ("live_small", "covered")


def test_an_unreadable_switch_closes_the_gate(state):
    def boom() -> bool:
        raise RuntimeError("config broken")

    assert not gate_lookup(state, "pf_default", live=boom)().allowed


def test_the_default_settings_keep_options_off():
    from stonks.config import load_settings

    assert load_settings().production.options.live is False
