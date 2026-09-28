"""Factor routes over the API: the catalog, one factor, formula checks,
values at a date, and a tear sheet job with its typed result."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from stonks.api import create_app
from tests.integration.app.test_api import AUTH, LOOPBACK, _wait_job
from tests.integration.app.test_factors_service import TICKERS, seed_factor_lake


@pytest.fixture
def client(settings, seeded, fake_source):
    settings.api.allowed_hosts = ["testserver"]
    seed_factor_lake(settings.lake.path)
    app = create_app(settings, source_factory=lambda: fake_source)
    with TestClient(app, client=LOOPBACK) as c:
        yield c


def test_catalog_and_one_factor(client):
    body = client.get("/api/factors", params={"set": "classic"}).json()
    assert {f["id"] for f in body["factors"]} >= {"mom_12_1", "low_vol_60"}
    assert any(s["name"] == "alpha158" and s["count"] == 157 for s in body["sets"])
    assert client.get("/api/factors", params={"set": "nope"}).status_code == 404
    one = client.get("/api/factors/KMID").json()
    assert one["expression"] == "Div(Sub($close,$open),$open)"
    assert client.get("/api/factors/nope").status_code == 404


def test_check_and_values(client):
    ok = client.post(
        "/api/factors/check", json={"expression": "Mean($close,5)/$close"}, headers=AUTH
    )
    assert ok.status_code == 200 and ok.json()["ok"] is True
    bad = client.post("/api/factors/check", json={"expression": "Ref($close,-1)"}, headers=AUTH)
    assert bad.json()["ok"] is False
    values = client.post(
        "/api/factors/values",
        json={"factor": "ROC20", "universe": TICKERS, "as_of": "2026-03-02"},
        headers=AUTH,
    )
    assert values.status_code == 200, values.text
    assert len(values.json()["values"]) == len(TICKERS)
    both = client.post(
        "/api/factors/values",
        json={"factor": "ROC20", "universe": TICKERS, "universe_id": "fac", "as_of": "2026-03-02"},
        headers=AUTH,
    )
    assert both.status_code == 422
    formula = client.post(
        "/api/factors/values",
        json={"factor": "$close > 3", "universe": TICKERS, "as_of": "2026-03-02"},
        headers=AUTH,
    )
    assert formula.status_code == 422


def test_tearsheet_job_and_typed_result(client):
    body = {
        "factor": "mom_6_1",
        "universe": TICKERS,
        "start": "2025-09-01",
        "end": "2026-03-31",
        "horizons": [1, 5],
    }
    assert client.post("/api/factors/tearsheets", json=body).status_code == 401
    resp = client.post("/api/factors/tearsheets", json=body, headers=AUTH)
    assert resp.status_code == 202, resp.text
    job = resp.json()
    assert job["kind"] == "factor_tearsheet"
    assert _wait_job(client, job["id"])["status"] == "succeeded"
    result = client.get(f"/api/factors/tearsheets/{job['id']}/result")
    assert result.status_code == 200, result.text
    sheet = result.json()
    assert sheet["status"] == "ok"
    assert sheet["factor"]["id"] == "mom_6_1"
    assert [h["horizon"] for h in sheet["horizons"]] == [1, 5]
    assert set(sheet["ic_by_group"]) == {"sector", "asset_class", "size"}
    bad = client.post(
        "/api/factors/tearsheets", json=body | {"factor": "Ref($close,-2)"}, headers=AUTH
    )
    assert bad.status_code == 422
    inverted = client.post(
        "/api/factors/tearsheets", json=body | {"start": "2026-04-01"}, headers=AUTH
    )
    assert inverted.status_code == 422
