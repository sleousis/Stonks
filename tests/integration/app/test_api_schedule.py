"""Scheduling over the API: Prometheus metrics (scrape token or loopback),
readiness and liveness probes, the schedule (next and recent runs), an
audited run-now, and the in-process scheduler hosted by the lifespan."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from stonks.accounts import DEFAULT_OWNER_ID
from stonks.api import create_app
from stonks.app.context import AppContext
from stonks.app.schedule import ScheduleService
from stonks.app.services import Services
from stonks.scheduling.config import IntervalTriggerConfig, JobConfig, SchedulerConfig
from stonks.store.state import SqliteState
from tests.integration.app.test_api import AUTH, LOOPBACK, REMOTE

METRICS_TOKEN = "scrape-token-4242"


def _config(tmp_path, backend: str = "local") -> SchedulerConfig:
    return SchedulerConfig(
        backend=backend,  # type: ignore[arg-type]
        lock_path=tmp_path / "scheduler.lock",
        watchdog_seconds=0.05,
        jobs=[
            JobConfig(
                name="health",
                action="health",
                trigger=IntervalTriggerConfig(every_minutes=10_000),
            ),
        ],
    )


def _services(settings, fake_source, config: SchedulerConfig) -> Services:
    settings.api.allowed_hosts = ["testserver"]
    ctx = AppContext(settings, source_factory=lambda: fake_source)
    svc = Services.create(ctx)
    svc.schedule = ScheduleService(ctx, config=config)
    svc.schedule.bind(svc)
    return svc


@pytest.fixture
def services(settings, seeded, fake_source, tmp_path):
    return _services(settings, fake_source, _config(tmp_path))


@pytest.fixture
def client(settings, services):
    with TestClient(create_app(settings, services=services), client=LOOPBACK) as c:
        yield c


@pytest.fixture
def remote(settings, services):
    with TestClient(create_app(settings, services=services), client=REMOTE) as c:
        yield c


# ---- metrics ------------------------------------------------------------------------


def test_metrics_open_on_loopback_without_a_scrape_token(client, monkeypatch):
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert "stonks_" in resp.text


def test_metrics_refused_remotely_without_a_token(remote):
    resp = remote.get("/metrics")
    assert resp.status_code == 401
    # The API token is not a scrape credential unless it's configured as one.
    assert remote.get("/metrics", headers=AUTH).status_code == 401


def test_metrics_scrape_token(settings, services, monkeypatch):
    monkeypatch.setenv("STONKS_METRICS_TOKEN", METRICS_TOKEN)
    app = create_app(settings, services=services)
    with TestClient(app, client=REMOTE) as remote:
        ok = remote.get("/metrics", headers={"Authorization": f"Bearer {METRICS_TOKEN}"})
        assert ok.status_code == 200
        assert remote.get("/metrics").status_code == 401
        bad = remote.get("/metrics", headers={"Authorization": "Bearer nope"})
        assert bad.status_code == 401


def test_metrics_loopback_can_be_closed(settings, services, monkeypatch):
    monkeypatch.setenv("STONKS_METRICS_TOKEN", METRICS_TOKEN)
    monkeypatch.setenv("STONKS_METRICS_ALLOW_LOOPBACK", "false")
    with TestClient(create_app(settings, services=services), client=LOOPBACK) as c:
        assert c.get("/metrics").status_code == 401
        ok = c.get("/metrics", headers={"Authorization": f"Bearer {METRICS_TOKEN}"})
        assert ok.status_code == 200


def test_metrics_not_in_the_openapi_spec(client):
    assert "/metrics" not in client.get("/openapi.json").json()["paths"]


# ---- probes -------------------------------------------------------------------------


def test_probes_are_public(remote):
    live = remote.get("/api/health/live")
    assert live.status_code == 200
    assert live.json()["status"] == "ok"
    ready = remote.get("/api/health/ready")
    assert ready.status_code == 200, ready.text
    assert ready.json()["checks"] == {"state": "ok", "lake": "ok"}


def test_readiness_fails_without_leaking_paths(settings, services, tmp_path):
    with TestClient(create_app(settings, services=services), client=REMOTE) as remote:
        settings.lake.path.unlink()
        resp = remote.get("/api/health/ready")
    assert resp.status_code == 503
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert "lake" in resp.json()["detail"]
    assert str(tmp_path) not in resp.text


# ---- schedule -----------------------------------------------------------------------


def test_schedule_lists_jobs_and_recent_runs(client):
    resp = client.get("/api/schedule")
    assert resp.status_code == 200
    body = resp.json()
    assert body["hosted"] is False
    [job] = body["jobs"]
    assert job["name"] == "health"
    assert job["action"] == "health"
    assert job["next_run_at"] is not None
    assert body["recent"] == []


def _sessions_at(settings, fake_source, tmp_path, now):
    svc = _services(settings, fake_source, _config(tmp_path))
    svc.schedule = ScheduleService(svc.context, config=_config(tmp_path), clock=lambda: now)
    svc.schedule.bind(svc)
    with TestClient(create_app(settings, services=svc), client=LOOPBACK) as c:
        return c.get("/api/schedule").json()["market"]


def test_schedule_gives_todays_and_the_next_market_session(settings, seeded, fake_source, tmp_path):
    from datetime import UTC, datetime

    # Friday 2026-09-25, 12:00 UTC: before the NYSE open (13:30 UTC in summer).
    market = _sessions_at(settings, fake_source, tmp_path, datetime(2026, 9, 25, 12, tzinfo=UTC))
    assert market["calendar"] == "XNYS" and market["is_open"] is False
    today = market["today"]
    assert today["date"] == "2026-09-25"
    assert today["open"].startswith("2026-09-25T13:30:00")
    assert today["close"].startswith("2026-09-25T20:00:00")
    assert today["pre_open"].startswith("2026-09-25T13:00:00")
    assert market["next"]["date"] == "2026-09-28"
    assert market["next"]["open"].startswith("2026-09-28T13:30:00")


def test_schedule_has_no_session_today_on_a_weekend(settings, seeded, fake_source, tmp_path):
    from datetime import UTC, datetime

    market = _sessions_at(settings, fake_source, tmp_path, datetime(2026, 9, 26, 15, tzinfo=UTC))
    assert market["today"] is None and market["is_open"] is False
    assert market["next"]["date"] == "2026-09-28"


def test_run_now_is_audited_and_recorded(client, settings):
    resp = client.post("/api/schedule/health/run-now", json={}, headers=AUTH)
    assert resp.status_code == 202, resp.text
    started = resp.json()
    assert started["job"] == "health"
    assert started["run_key"].startswith("manual:")

    deadline = time.monotonic() + 30
    runs: list[dict] = []
    while time.monotonic() < deadline:
        runs = client.get("/api/schedule").json()["recent"]
        if runs and runs[0]["status"] not in ("running",):
            break
        time.sleep(0.05)
    assert runs and runs[0]["run_key"] == started["run_key"]
    assert runs[0]["status"] in ("succeeded", "skipped", "failed")

    with SqliteState(settings.state.path) as state:
        rows = state.sql("SELECT actor, target_id FROM audit_log WHERE action = 'schedule.run_now'")
    assert [(r["actor"], r["target_id"]) for r in rows] == [(f"user:{DEFAULT_OWNER_ID}", "health")]


def test_run_now_needs_the_token_and_a_known_job(client):
    assert client.post("/api/schedule/health/run-now", json={}).status_code == 401
    unknown = client.post("/api/schedule/nope/run-now", json={}, headers=AUTH)
    assert unknown.status_code == 404


# ---- hosted scheduler ---------------------------------------------------------------


def test_lifespan_hosts_the_in_process_scheduler(settings, seeded, fake_source, tmp_path):
    services = _services(settings, fake_source, _config(tmp_path, backend="in_process"))
    with TestClient(create_app(settings, services=services), client=LOOPBACK) as c:
        assert c.get("/api/schedule").json()["hosted"] is True
        deadline = time.monotonic() + 10
        live = c.get("/api/health/live")
        while live.status_code != 200 and time.monotonic() < deadline:
            time.sleep(0.05)  # the scheduler thread registers itself on start
            live = c.get("/api/health/live")
        assert live.status_code == 200, live.text
        assert live.json()["checks"] == {"process": "ok", "scheduler": "ok"}
    assert services.schedule.handle is None  # stopped with the app


def test_schedule_says_when_each_job_runs_in_plain_words(client):
    [job] = client.get("/api/schedule", headers=AUTH).json()["jobs"]
    assert job["trigger"] == "every 10000 min"
    assert job["trigger_text"] == "Every 10000 minutes"
