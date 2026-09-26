"""REST routes added by integration step 4: server-side backups (the
scheduler's api backend calls them) and the statement-audit flags."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from stonks.accounts import Role
from stonks.api import create_app
from stonks.api.deps import current_principal
from stonks.auth import Principal
from stonks.store.audit import audit_statements
from stonks.store.lake import DuckDBLake
from tests.integration.app.test_api import AUTH, LOOPBACK, REMOTE, _wait_job


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


# ---- backups ------------------------------------------------------------------------


def test_an_admin_takes_a_backup_and_reads_its_result(client):
    resp = client.post("/api/backups", headers=AUTH)
    assert resp.status_code == 202, resp.text
    job = resp.json()
    assert job["kind"] == "backup"
    assert resp.headers["location"] == f"/api/jobs/{job['id']}"
    done = _wait_job(client, job["id"])
    assert done["status"] == "succeeded", done["error"]
    result = client.get(f"/api/backups/jobs/{job['id']}/result", headers=AUTH)
    assert result.status_code == 200
    body = result.json()
    assert body["backup_id"] and body["pruned"] == []


def test_a_backup_needs_a_credential(remote):
    assert remote.post("/api/backups").status_code == 401


def test_a_backup_needs_an_admin(app, remote):
    trader = Principal.create(
        user_id="usr_trader", kind="human", role=Role.TRADER, scopes=["read", "trade", "lab"],
        mfa_fresh=False, via="token",
    )  # fmt: skip
    app.dependency_overrides[current_principal] = lambda: trader
    try:
        assert remote.post("/api/backups", headers=AUTH).status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_the_result_of_another_job_kind_is_not_found(client):
    job = client.post(
        "/api/ingest/runs", json={"kind": "prices", "tickers": ["NEW.US"]}, headers=AUTH
    ).json()
    _wait_job(client, job["id"])
    resp = client.get(f"/api/backups/jobs/{job['id']}/result", headers=AUTH)
    assert resp.status_code == 404
    # admin-only, reads included: no loopback exemption
    assert client.get(f"/api/backups/jobs/{job['id']}/result").status_code == 401


# ---- statement flags ------------------------------------------------------------------


@pytest.fixture
def flagged(settings, seeded):
    lake = DuckDBLake(settings.lake.path)
    try:
        lake.upsert_balance_sheet(
            pd.DataFrame(
                [
                    {
                        "ticker": ticker,
                        "period_end": date(2024, 12, 31),
                        "frequency": "A",
                        "filing_date": date(2025, 2, 1),
                        "total_assets": 1000.0,
                        "total_liabilities": liabilities,
                        "total_stockholder_equity": 400.0,
                    }
                    for ticker, liabilities in (("UP.US", 100.0), ("DOWN.US", 200.0))
                ]
            )
        )
        audit_statements(lake)
    finally:
        lake.close()


def test_statement_flags_list_and_filter(client, flagged):
    body = client.get("/api/statements/flags").json()
    assert body["total"] == 2
    first = body["items"][0]
    assert first["ticker"] == "DOWN.US" and first["check_id"] == "balance_identity"
    assert first["severity"] == "error" and first["period_end"] == "2024-12-31"
    assert first["detail"]

    only_up = client.get("/api/statements/flags", params={"ticker": "UP.US"}).json()
    assert [f["ticker"] for f in only_up["items"]] == ["UP.US"]
    warnings = client.get("/api/statements/flags", params={"severity": "warning"}).json()
    assert warnings["total"] == 0
    paged = client.get("/api/statements/flags", params={"limit": 1, "offset": 1}).json()
    assert paged["total"] == 2 and [f["ticker"] for f in paged["items"]] == ["UP.US"]


def test_statement_flags_are_empty_without_an_audit(client):
    assert client.get("/api/statements/flags").json() == {
        "items": [],
        "total": 0,
        "limit": 50,
        "offset": 0,
    }
