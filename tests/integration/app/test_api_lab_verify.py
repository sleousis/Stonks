"""Roadmap 23.9 through the REST API: queue a lab verify job and read its
result, and a clear error for an unknown target."""

from __future__ import annotations

from tests.integration.app.test_api import AUTH, _wait_job


def _verify(client, body):
    resp = client.post("/api/lab/verify", json=body, headers=AUTH)
    assert resp.status_code == 202, resp.text
    return _wait_job(client, resp.json()["id"])


def test_verify_the_active_strategies(client, seeded):
    job = _verify(client, {})
    assert job["status"] == "succeeded", job
    result = client.get(f"/api/lab/verify/jobs/{job['id']}/result", headers=AUTH).json()
    assert result["checked"] == 1 and result["moved"] == [] and result["alerted"] is False
    (report,) = result["reports"]
    assert report["target"] == seeded["active_id"] and report["kind"] == "strategy"
    assert report["restated_tickers"] == []


def test_an_unknown_target_fails_the_job(client, seeded):
    job = _verify(client, {"targets": ["nope"]})
    assert job["status"] == "failed"
    assert "no lab run or strategy" in (job.get("error") or "")


def test_the_request_is_strict(client, seeded):
    resp = client.post("/api/lab/verify", json={"tolerance": -1}, headers=AUTH)
    assert resp.status_code == 422
