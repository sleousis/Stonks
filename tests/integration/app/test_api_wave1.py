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
