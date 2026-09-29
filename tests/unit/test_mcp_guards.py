"""Pure helpers behind the guarded MCP write tools."""

from __future__ import annotations

import pytest

from stonks.mcp.guards import (
    draft_preview,
    lab_registration_preview,
    live_trading_state,
    status_change_preview,
)

STRATEGY = {
    "id": "s1",
    "class_path": "pkg.mod:Cls",
    "status": "shadow",
    "params": {"a": 1},
    "survival_reports": [
        {"test_id": "oos", "passed": True, "metrics": {}, "notes": ""},
        {"test_id": "drift", "passed": False, "metrics": {}, "notes": ""},
    ],
}


def _broker(kind="simulated", paper=True, allow_live=False, creds=False):
    """The GET /api/brokers payload (``BrokerInfo``)."""
    return {
        "kind": kind,
        "paper": paper,
        "allow_live": allow_live,
        "credentials_configured": creds,
    }


@pytest.mark.parametrize(
    ("info", "expected"),
    [
        (_broker("simulated"), "off"),
        # the simulated broker ignores paper, so it is never real money
        (_broker("simulated", paper=False, allow_live=True), "off"),
        (_broker("alpaca", paper=True), "off"),
        (_broker("alpaca", paper=True, allow_live=True, creds=True), "off"),
        # a non-paper Alpaca endpoint is refused whatever allow_live says
        (_broker("alpaca", paper=False, allow_live=True, creds=True), "on"),
        (_broker("alpaca", paper=False, allow_live=False), "on"),
        ({"kind": "alpaca"}, "unknown"),
        ({"kind": "alpaca", "paper": "yes"}, "unknown"),
        ({"kind": "somethingelse", "paper": True}, "unknown"),
        # speculative keys the real route does not send are not trusted
        ({"live": False}, "unknown"),
        (None, "unknown"),
        ([], "unknown"),
        ({}, "unknown"),
    ],
)
def test_live_trading_state(info, expected):
    assert live_trading_state(info) == expected


def test_status_change_preview_describes_transition_and_reports():
    preview = status_change_preview(STRATEGY, "active")
    assert preview["preview"] is True
    assert preview["applied"] is False
    assert preview["strategy_id"] == "s1"
    assert preview["current_status"] == "shadow"
    assert preview["new_status"] == "active"
    assert preview["survival"] == {"passed": ["oos"], "failed": ["drift"]}
    assert "confirm=true" in preview["next_step"]
    assert any("drift" in w for w in preview["warnings"])


def test_status_change_preview_flags_noop():
    preview = status_change_preview({**STRATEGY, "status": "active"}, "active")
    assert any("already" in w for w in preview["warnings"])


DRAFT = {
    "id": "d1",
    "name": "trend",
    "kind": "rule",
    "spec": {"version": 1},
    "source_code": None,
    "status": "draft",
    "registered_strategy_id": None,
    "strategy_status": None,
}
REGISTERED = {**DRAFT, "status": "registered", "registered_strategy_id": "s1"}


def test_register_preview_lands_in_shadow():
    preview = draft_preview(DRAFT, "register", None)
    assert preview["preview"] is True and preview["applied"] is False
    assert preview["action"] == "register"
    assert preview["new_status"] == "shadow"
    assert preview["draft"]["id"] == "d1"
    assert "confirm=true" in preview["next_step"]
    assert not any("already" in w for w in preview["warnings"])


def test_register_preview_flags_already_registered():
    preview = draft_preview(REGISTERED, "register", None)
    assert any("already registered as s1" in w for w in preview["warnings"])


@pytest.mark.parametrize("action", ["enable", "disable"])
def test_enable_disable_preview_needs_registration(action):
    preview = draft_preview(DRAFT, action, None)
    assert any("not registered" in w for w in preview["warnings"])
    assert preview["strategy"] is None


def test_enable_preview_carries_strategy_survival_and_warnings():
    preview = draft_preview(REGISTERED, "enable", STRATEGY)
    assert preview["new_status"] == "active"
    assert preview["strategy"]["current_status"] == "shadow"
    assert preview["strategy"]["survival"] == {"passed": ["oos"], "failed": ["drift"]}
    assert any("drift" in w for w in preview["warnings"])
    assert any("traded" in w for w in preview["warnings"])


def test_disable_preview_moves_to_shadow():
    preview = draft_preview(REGISTERED, "disable", {**STRATEGY, "status": "active"})
    assert preview["new_status"] == "shadow"
    assert preview["strategy"]["current_status"] == "active"


def test_lab_registration_preview_shows_request_and_lands_in_shadow():
    preview = lab_registration_preview({"budget": 2, "register_strategy": True})
    assert preview["preview"] is True and preview["applied"] is False
    assert preview["request"] == {"budget": 2, "register_strategy": True}
    assert any("on trial" in w and "shadow" not in w for w in preview["warnings"])
    assert "confirm=true" in preview["next_step"]


def test_draft_preview_never_echoes_source_code():
    code = {**REGISTERED, "kind": "code", "source_code": "import os"}
    assert "import os" not in str(draft_preview(code, "register", None))
