"""REST API routes for the Wave 1 features: sources, risk, shadow, health
report, P&L, brokers, cost models, typed job results and stream tokens."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from stonks.api import create_app
from tests.integration.app.test_api import AUTH, LOOPBACK, REMOTE


@pytest.fixture
def app(settings, seeded, fake_source):
    settings.api.allowed_hosts = ["testserver"]
    return create_app(settings, source_factory=lambda: fake_source, sse_poll_seconds=0.02)


@pytest.fixture
def client(app):
    with TestClient(app, client=LOOPBACK) as c:
        yield c


@pytest.fixture
def remote(app):
    with TestClient(app, client=REMOTE) as c:
        yield c


# ---- sources ----------------------------------------------------------------


def test_list_data_sources(client):
    resp = client.get("/api/sources")
    assert resp.status_code == 200
    by_id = {s["id"]: s for s in resp.json()}
    assert set(by_id) == {"eodhd", "yahoo"}
    assert by_id["eodhd"]["default"] is True
    assert by_id["eodhd"]["configured"] is False  # no key in the test settings


def test_ingest_request_accepts_source(client):
    resp = client.post(
        "/api/ingest/runs",
        json={"kind": "prices", "tickers": ["NEW.US"], "source": "yahoo"},
        headers=AUTH,
    )
    assert resp.status_code == 202
    bad = client.post(
        "/api/ingest/runs",
        json={"kind": "prices", "tickers": ["NEW.US"], "source": "bogus"},
        headers=AUTH,
    )
    assert bad.status_code == 422


# ---- typed job results --------------------------------------------------------

BAH = "stonks.strategies.examples.buy_and_hold:BuyAndHold"


def _backtest_body() -> dict:
    return {
        "strategy": {"class_path": BAH, "params": {"ticker": "UP.US", "allocation": 1.0}},
        "universe": ["UP.US"],
        "start": "2025-10-01",
        "end": "2026-04-01",
    }


def _done(client: TestClient, job_id: str) -> dict:
    from tests.integration.app.test_api import _wait_job

    return _wait_job(client, job_id)


def test_typed_backtest_result_route(client):
    job = client.post("/api/lab/backtests", json=_backtest_body(), headers=AUTH).json()
    assert _done(client, job["id"])["status"] == "succeeded"
    resp = client.get(f"/api/lab/backtests/{job['id']}/result")
    assert resp.status_code == 200
    body = resp.json()
    assert body["final_return"] > 0.5
    assert body["equity"][0]["value"] == pytest.approx(10_000.0)
    # wrong kind is not found
    assert client.get(f"/api/lab/runs/{job['id']}/result").status_code == 404
    assert client.get("/api/lab/backtests/job_missing/result").status_code == 404


def test_typed_result_of_unfinished_job_is_conflict(client, app):
    orphan = app.state.services.runner.store.create("backtest", {})
    resp = client.get(f"/api/lab/backtests/{orphan.id}/result")
    assert resp.status_code == 409
    assert "queued" in resp.json()["detail"]


def test_typed_ingest_and_tick_result_routes(client):
    ingest = client.post(
        "/api/ingest/runs", json={"kind": "prices", "tickers": ["NEW.US"]}, headers=AUTH
    ).json()
    assert _done(client, ingest["id"])["status"] == "succeeded"
    body = client.get(f"/api/ingest/jobs/{ingest['id']}/result").json()
    assert body["tickers_ok"] == 1

    tick = client.post(
        "/api/ticks",
        json={"dry_run": True, "tickers": ["UP.US"], "as_of": "2026-03-27"},
        headers=AUTH,
    ).json()
    assert _done(client, tick["id"])["status"] == "succeeded"
    body = client.get(f"/api/ticks/jobs/{tick['id']}/result").json()
    assert body["dry_run"] is True


def test_tick_runs_have_a_typed_summary(client, seeded):
    page = client.get("/api/ticks").json()
    summary = page["items"][0]["summary"]
    assert summary["orders_placed"] >= 0
    assert isinstance(summary["risk_adjustments"], list)
    assert {s["strategy_id"] for s in summary["shadow"]} == {"bah_shadow"}
    detail = client.get(f"/api/ticks/{seeded['tick_id']}").json()
    assert detail["summary"]["winner_strategy_id"] == seeded["active_id"]


def test_job_result_and_tick_summary_schemas_are_in_openapi(app):
    schemas = app.openapi()["components"]["schemas"]
    for name in (
        "BacktestResult",
        "LabRunView",
        "TickResultView",
        "IngestResultView",
        "TickSummary",
        "RiskAdjustmentView",
        "ShadowOutcomeView",
    ):
        assert name in schemas, name
    tick_run = schemas["TickRun"]["properties"]["summary"]
    assert "TickSummary" in str(tick_run)
