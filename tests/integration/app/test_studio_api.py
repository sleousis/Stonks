"""REST routes under /api/studio."""

from __future__ import annotations

import sys
import time

import pytest
from fastapi.testclient import TestClient

from stonks.api import create_app
from stonks.app.context import AppContext
from stonks.app.services import Services, default_strategy_sources
from stonks.app.studio import USER_MODULE_PREFIX, RuleStrategySource
from tests.integration.app.conftest import API_TOKEN
from tests.integration.app.test_studio import GOOD_CODE, TREND

LOOPBACK = ("127.0.0.1", 50000)
REMOTE = ("203.0.113.7", 50000)
AUTH = {"Authorization": f"Bearer {API_TOKEN}"}


@pytest.fixture(autouse=True)
def _forget_user_modules():
    yield
    for name in [m for m in sys.modules if m.startswith(USER_MODULE_PREFIX)]:
        del sys.modules[name]


@pytest.fixture
def app(settings, seeded, fake_source):
    settings.api.allowed_hosts = ["testserver"]
    services = Services.create(
        AppContext(settings, source_factory=lambda: fake_source),
        strategy_sources=[*default_strategy_sources(), RuleStrategySource()],
    )
    return create_app(settings, services=services)


def _wait_job(client: TestClient, job_id: str, timeout: float = 120) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("succeeded", "failed", "cancelled"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


def _create(client, **body) -> dict:
    resp = client.post("/api/studio/drafts", json={"name": "t", "spec": TREND} | body, headers=AUTH)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_templates_and_schema_are_reads(client):
    templates = client.get("/api/studio/templates").json()
    assert {t["id"] for t in templates} == {
        "rsi_mean_reversion",
        "sma_trend_following",
        "donchian_breakout",
    }
    schema = client.get("/api/studio/schema").json()
    assert schema["title"] == "RuleSpec"
    assert "$defs" in schema


def test_validate_spec_route(client):
    resp = client.post("/api/studio/spec/validate", json={"spec": TREND}, headers=AUTH)
    assert resp.json() == {"valid": True, "issues": [], "smoke": None}
    bad = client.post(
        "/api/studio/spec/validate",
        json={"spec": {**TREND, "rank": {"by": "x"}}},
        headers=AUTH,
    ).json()
    assert bad["valid"] is False
    assert bad["issues"][0]["path"] == "rank.by"


def test_studio_mutations_need_the_token(app, client):
    draft = _create(client)
    remote = TestClient(app, client=REMOTE)
    assert remote.get("/api/studio/templates").status_code == 401
    urls = [
        ("POST", "/api/studio/drafts"),
        ("PATCH", f"/api/studio/drafts/{draft['id']}"),
        ("DELETE", f"/api/studio/drafts/{draft['id']}"),
        ("POST", f"/api/studio/drafts/{draft['id']}/validate"),
        ("POST", f"/api/studio/drafts/{draft['id']}/backtests"),
        ("POST", f"/api/studio/drafts/{draft['id']}/lab-runs"),
        ("POST", f"/api/studio/drafts/{draft['id']}/register"),
        ("POST", f"/api/studio/drafts/{draft['id']}/enable"),
        ("POST", f"/api/studio/drafts/{draft['id']}/disable"),
        ("POST", "/api/studio/spec/validate"),
    ]
    for method, url in urls:
        assert client.request(method, url, json={}).status_code == 401, (method, url)
    # nothing changed
    assert client.get(f"/api/studio/drafts/{draft['id']}").json()["name"] == "t"


def test_draft_crud_routes(client):
    draft = _create(client, name="Trend")
    assert draft["kind"] == "rule" and draft["status"] == "draft"
    listed = client.get("/api/studio/drafts").json()
    assert listed["total"] == 1
    patched = client.patch(
        f"/api/studio/drafts/{draft['id']}", json={"name": "Trend 2"}, headers=AUTH
    ).json()
    assert patched["name"] == "Trend 2"
    assert client.get(f"/api/studio/drafts/{draft['id']}").json()["name"] == "Trend 2"
    deleted = client.delete(f"/api/studio/drafts/{draft['id']}", headers=AUTH)
    assert deleted.status_code == 200
    missing = client.get(f"/api/studio/drafts/{draft['id']}")
    assert missing.status_code == 404
    assert missing.headers["content-type"].startswith("application/problem+json")


def test_create_validation_error_is_422(client):
    resp = client.post("/api/studio/drafts", json={"name": ""}, headers=AUTH)
    assert resp.status_code == 422


def test_validate_route(client):
    draft = _create(client)
    body = client.post(
        f"/api/studio/drafts/{draft['id']}/validate",
        json={"tickers": ["UP.US"], "as_of": "2026-03-02", "bars": 5},
        headers=AUTH,
    ).json()
    assert body["valid"] is True
    assert body["smoke"]["data"] == "lake"
    assert body["smoke"]["signals"] == 5
    no_body = client.post(f"/api/studio/drafts/{draft['id']}/validate", headers=AUTH)
    assert no_body.status_code == 200
    assert no_body.json()["smoke"]["data"] == "sample"


def test_backtest_register_enable_disable_flow(client):
    draft = _create(client, name="Flow")
    resp = client.post(
        f"/api/studio/drafts/{draft['id']}/backtests",
        json={"universe": ["UP.US", "DOWN.US"], "start": "2025-11-01", "end": "2026-04-01"},
        headers=AUTH,
    )
    assert resp.status_code == 202, resp.text
    assert resp.headers["location"] == f"/api/jobs/{resp.json()['id']}"
    done = _wait_job(client, resp.json()["id"])
    assert done["status"] == "succeeded", done["error"]
    assert done["result"]["equity"]

    registered = client.post(f"/api/studio/drafts/{draft['id']}/register", headers=AUTH).json()
    sid = registered["registered_strategy_id"]
    assert registered["strategy_status"] == "shadow"
    assert client.get(f"/api/strategies/{sid}").json()["status"] == "shadow"
    again = client.post(f"/api/studio/drafts/{draft['id']}/register", headers=AUTH)
    assert again.status_code == 409

    # enabling is a promotion, gated by go-live (BL-24): refused without a
    # paper period and no override in the body
    on = client.post(f"/api/studio/drafts/{draft['id']}/enable", headers=AUTH)
    assert on.status_code == 409
    assert "go-live" in on.json()["detail"]
    assert client.get(f"/api/strategies/{sid}").json()["status"] == "shadow"


def test_lab_run_route(client):
    draft = _create(client)
    resp = client.post(
        f"/api/studio/drafts/{draft['id']}/lab-runs",
        json={
            "universe": ["UP.US", "DOWN.US"],
            "start": "2025-10-01",
            "end": "2026-04-01",
            "budget": 1,
            "survival_tests": ["oos"],
        },
        headers=AUTH,
    )
    assert resp.status_code == 202, resp.text
    done = _wait_job(client, resp.json()["id"])
    assert done["status"] == "succeeded", done["error"]
    assert done["result"]["verdict"] in ("pass", "fail")


def test_invalid_spec_backtest_is_422(client):
    draft = _create(client, spec={"version": 1})
    resp = client.post(
        f"/api/studio/drafts/{draft['id']}/backtests",
        json={"universe": ["UP.US"], "start": "2025-11-01", "end": "2026-04-01"},
        headers=AUTH,
    )
    assert resp.status_code == 422
    assert "indicators" in resp.json()["detail"]


def test_code_drafts_are_403_when_disabled(client, settings):
    resp = client.post(
        "/api/studio/drafts",
        json={"name": "c", "kind": "code", "source_code": GOOD_CODE},
        headers=AUTH,
    )
    assert resp.status_code == 403
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert "allow_code_strategies" in resp.json()["detail"]

    settings.api.allow_code_strategies = True
    draft = _create(client, name="c", kind="code", source_code=GOOD_CODE, spec={})
    settings.api.allow_code_strategies = False
    for method, suffix in [
        ("GET", ""),
        ("PATCH", ""),
        ("DELETE", ""),
        ("POST", "/validate"),
        ("POST", "/backtests"),
        ("POST", "/lab-runs"),
        ("POST", "/register"),
        ("POST", "/enable"),
        ("POST", "/disable"),
    ]:
        body = (
            {"universe": ["UP.US"], "start": "2025-11-01", "end": "2026-04-01"}
            if suffix in ("/backtests", "/lab-runs")
            else {}
        )
        r = client.request(
            method, f"/api/studio/drafts/{draft['id']}{suffix}", json=body, headers=AUTH
        )
        assert r.status_code == 403, (method, suffix, r.status_code)


def test_code_draft_flow_when_enabled(client, settings):
    settings.api.allow_code_strategies = True
    draft = _create(client, name="c", kind="code", source_code=GOOD_CODE, spec={})
    assert draft["source_code"] == GOOD_CODE
    result = client.post(f"/api/studio/drafts/{draft['id']}/validate", headers=AUTH).json()
    assert result["valid"] is True, result
    registered = client.post(f"/api/studio/drafts/{draft['id']}/register", headers=AUTH)
    assert registered.status_code == 200, registered.text
