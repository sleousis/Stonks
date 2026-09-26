"""Routes the console needed (UI step for Waves 1 and 2): lab sweeps with
results, survival test option schemas from the backend, and structured
failing checks on a refused promotion."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from stonks.api import create_app
from stonks.lab.parallel import ParallelSettings
from tests.integration.app.test_api import AUTH, LOOPBACK


@pytest.fixture
def client(settings, seeded, fake_source):
    settings.api.allowed_hosts = ["testserver"]
    settings.lab.parallel = ParallelSettings(max_workers=1)
    app = create_app(settings, source_factory=lambda: fake_source, sse_poll_seconds=0.02)
    with TestClient(app, client=LOOPBACK) as c:
        yield c


def _wait(client: TestClient, job_id: str, timeout: float = 120) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("succeeded", "failed", "cancelled"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


# ---- structured failing checks -----------------------------------------------------------


def test_refused_promotion_lists_the_failing_checks(client, seeded):
    resp = client.post(f"/api/strategies/{seeded['shadow_id']}/promote", headers=AUTH)
    assert resp.status_code == 409
    body = resp.json()
    assert "go-live" in body["detail"]
    checks = body["failing_checks"]
    assert checks and all({"name", "detail"} <= set(c) for c in checks)
    assert {c["name"] for c in checks} >= {"min_days"}
    assert all(isinstance(c["name"], str) and c["name"] for c in checks)


def test_other_conflicts_carry_no_failing_checks(client, seeded):
    resp = client.post(f"/api/strategies/{seeded['active_id']}/promote", headers=AUTH)
    if resp.status_code == 409:
        assert "failing_checks" not in resp.json()


# ---- survival test options -----------------------------------------------------------------


def test_survival_tests_list_their_option_schemas(client):
    from stonks.lab.survival.registry import survival_test_names

    tests = client.get("/api/lab/survival-tests").json()
    by_id = {t["id"]: t for t in tests}
    assert set(by_id) == set(survival_test_names())
    oos = by_id["oos"]
    assert oos["options_schema"]["properties"]["mode"]["default"] == "psr"
    assert "quick" in oos["presets"] and oos["description"]
    # a test without an Options model still gets a schema from its constructor
    assert all(t["options_schema"]["type"] == "object" for t in tests)
    # the preset's own options are listed too
    promo = {p["name"]: p for p in client.get("/api/lab/survival-presets").json()}
    assert promo["promotion"]["options"]["mcpt"]["n_permutations"] == 200
    assert "oos" in promo["quick"]["tests"]


# ---- lab sweeps --------------------------------------------------------------------------------


def test_sweep_runs_as_a_job_with_typed_results(client):
    body = {
        "universe": ["UP.US", "DOWN.US"],
        "start": "2025-10-01",
        "end": "2026-04-01",
        "strategies": ["buy_and_hold", "momentum"],
        "budget": 1,
        "grid_size": 1,
        "survival_tests": ["oos"],
        "cost_model": "zero",
    }
    assert client.post("/api/lab/sweeps", json=body).status_code == 401
    resp = client.post("/api/lab/sweeps", json=body, headers=AUTH)
    assert resp.status_code == 202, resp.text
    done = _wait(client, resp.json()["id"])
    assert done["status"] == "succeeded", done["error"]
    result = client.get(f"/api/lab/sweeps/{done['id']}/result").json()
    rows = result["rows"]
    assert [(r["strategy"], r["ticker"]) for r in rows] == [
        ("buy_and_hold", "UP.US"),
        ("buy_and_hold", "DOWN.US"),
        ("momentum", None),
    ]
    assert result["passed"] + result["failed"] + result["errors"] == 3
    assert all(r["verdict"] in ("pass", "fail", "error") for r in rows)
    assert "oos" in rows[0]["survival"]


def test_sweep_rejects_registration_and_unknown_strategies(client):
    base = {"universe": ["UP.US"], "start": "2025-10-01", "end": "2026-04-01"}
    bad = client.post("/api/lab/sweeps", json={**base, "register_if_passes": True}, headers=AUTH)
    assert bad.status_code == 422
    unknown = client.post("/api/lab/sweeps", json={**base, "strategies": ["nope"]}, headers=AUTH)
    assert unknown.status_code == 422
