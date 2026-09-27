"""BE-46: an active strategy's go-live evidence is the default book's P&L.
Only that book's owner and admins see the figures; anyone else sees the
verdicts without the numbers."""

from __future__ import annotations

from tests.integration.app.test_api import AUTH

PNL_CHECKS = {"max_drawdown", "max_drift", "within_mc_band", "quit_rule"}


def _report(client, headers, strategy_id):
    response = client.get(f"/api/strategies/{strategy_id}/golive", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_be46_a_viewer_sees_no_default_book_figures(client, seeded, people):
    report = _report(client, people["vic"]["headers"], seeded["active_id"])
    assert report["source"] == "portfolio"
    hidden = [c for c in report["checks"] if c["name"] in PNL_CHECKS]
    assert hidden and all(c["value"] is None for c in hidden)
    assert all("hidden" in c["detail"] for c in hidden)
    assert report["costs"] is None


def test_be46_the_owner_sees_the_figures(client, seeded, people):
    report = _report(client, AUTH, seeded["active_id"])
    drawdown = next(c for c in report["checks"] if c["name"] == "max_drawdown")
    assert "hidden" not in drawdown["detail"]
