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
