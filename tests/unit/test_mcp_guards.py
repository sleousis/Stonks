"""Pure helpers behind the guarded MCP write tools."""

from __future__ import annotations

import pytest

from stonks.mcp.guards import live_trading_state, status_change_preview

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


@pytest.mark.parametrize(
    ("info", "expected"),
    [
        ({"live": False}, "off"),
        ({"live": True}, "on"),
        ({"live_trading": False}, "off"),
        ({"kind": "simulated"}, "off"),
        ({"kind": "alpaca", "paper": True}, "off"),
        ({"kind": "alpaca", "paper": False, "allow_live": True}, "on"),
        ({"kind": "alpaca", "paper": False, "allow_live": False}, "off"),
        ({"kind": "alpaca"}, "unknown"),
        ({"kind": "somethingelse"}, "unknown"),
        # "live" wins over contradicting fields
        ({"live": True, "kind": "simulated"}, "on"),
        ({"live": "no"}, "unknown"),
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
