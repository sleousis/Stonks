"""Universes over the REST API: create, list, show, members, refresh and
ensure data as background jobs, index import (roadmap 10.5)."""

from __future__ import annotations

import time
from datetime import date

import pytest
from fastapi.testclient import TestClient

from stonks.api import create_app
from stonks.ingest.schemas import SymbolListing
from tests.fixtures.universes import FakeListingSource, bars
from tests.integration.app.conftest import API_TOKEN

LOOPBACK = ("127.0.0.1", 50000)
AUTH = {"Authorization": f"Bearer {API_TOKEN}"}


@pytest.fixture
def listing_source() -> FakeListingSource:
    return FakeListingSource(
        [
            SymbolListing(ticker="AAA.US", security_type="common_stock"),
            SymbolListing(ticker="DEAD.US", security_type="common_stock", is_delisted=True),
        ],
        prices={
            "AAA.US": bars("AAA.US", date(2026, 3, 2), date(2026, 3, 31)),
            "DEAD.US": bars("DEAD.US", date(2026, 3, 2), date(2026, 3, 13)),
        },
    )


@pytest.fixture
def client(settings, seeded, listing_source):
    settings.api.allowed_hosts = ["testserver"]
    app = create_app(settings, source_factory=lambda: listing_source, sse_poll_seconds=0.02)
    with TestClient(app, client=LOOPBACK) as c:
        yield c


def _wait(client: TestClient, job_id: str, timeout: float = 60) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("succeeded", "failed", "cancelled"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


def test_create_list_show_and_members(client):
    body = {"id": "mine", "kind": "list", "name": "Mine", "spec": {"tickers": ["UP.US", "FLAT.US"]}}
    resp = client.post("/api/universes", json=body, headers=AUTH)
    assert resp.status_code == 201, resp.text
    assert resp.json()["refreshed_at"] is None
    assert client.post("/api/universes", json=body, headers=AUTH).status_code == 409

    listed = client.get("/api/universes").json()["items"]
    assert [u["id"] for u in listed] == ["mine"]

    job = client.post("/api/universes/mine/refresh", headers=AUTH)
    assert job.status_code == 202
    done = _wait(client, job.json()["id"])
    assert done["status"] == "succeeded", done["error"]
    result = client.get(f"/api/universes/refresh/{done['id']}/result").json()
    assert result["members"] == 2

    shown = client.get("/api/universes/mine").json()
    assert shown["member_count"] == 2 and shown["refreshed_at"] is not None
    members = client.get("/api/universes/mine/members", params={"as_of": "2026-01-02"}).json()
    assert members == {
        "universe_id": "mine",
        "as_of": "2026-01-02",
        "tickers": ["FLAT.US", "UP.US"],
        "count": 2,
    }


def test_create_from_csv(client):
    body = {"id": "csvu", "kind": "list", "csv": "ticker\nUP.US\nDOWN.US\n"}
    resp = client.post("/api/universes", json=body, headers=AUTH)
    assert resp.status_code == 201, resp.text
    assert resp.json()["spec"] == {"tickers": ["UP.US", "DOWN.US"]}


def test_bad_specs_and_unknown_ids(client):
    bad = {"id": "r", "kind": "rule", "spec": {"min_adv": 1}}  # no start
    assert client.post("/api/universes", json=bad, headers=AUTH).status_code == 422
    assert client.get("/api/universes/nope").status_code == 404
    assert client.get("/api/universes/nope/members").status_code == 404
    assert client.post("/api/universes/nope/refresh", headers=AUTH).status_code == 404
    unauth = {"id": "x", "kind": "list", "spec": {"tickers": ["A.US"]}}
    assert client.post("/api/universes", json=unauth).status_code == 401


def test_exchange_universe_refresh_and_ensure_data(client, listing_source):
    body = {
        "id": "us_all",
        "kind": "exchange",
        "spec": {"exchange": "US", "security_types": ["common_stock"]},
    }
    assert client.post("/api/universes", json=body, headers=AUTH).status_code == 201
    done = _wait(client, client.post("/api/universes/us_all/refresh", headers=AUTH).json()["id"])
    assert done["status"] == "succeeded", done["error"]
    assert listing_source.calls == ["US"]

    resp = client.post(
        "/api/universes/us_all/ensure",
        json={"start": "2026-03-02", "end": "2026-03-31"},
        headers=AUTH,
    )
    assert resp.status_code == 202, resp.text
    done = _wait(client, resp.json()["id"])
    assert done["status"] == "succeeded", done["error"]
    report = client.get(f"/api/universes/ensure/{done['id']}/result").json()
    # both members, the delisted one included, were fetched
    assert report["tickers_requested"] == 2 and report["tickers_fetched"] == 2
    assert {c[0] for c in listing_source.price_calls} == {"AAA.US", "DEAD.US"}


def test_index_import_then_index_universe(client):
    content = "date,ticker,action\n2026-01-02,UP.US,member\n2025-06-02,DOWN.US,remove\n"
    resp = client.post(
        "/api/universes/index-history",
        json={"index_id": "toy", "format": "csv", "content": content},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "index_id": "toy",
        "as_of": "2026-01-02",
        "constituents": 1,
        "changes": 1,
    }
    body = {"id": "toy", "kind": "index", "spec": {"index_id": "toy", "start_date": "2025-01-01"}}
    assert client.post("/api/universes", json=body, headers=AUTH).status_code == 201
    done = _wait(client, client.post("/api/universes/toy/refresh", headers=AUTH).json()["id"])
    assert done["status"] == "succeeded", done["error"]
    members = client.get("/api/universes/toy/members", params={"as_of": "2025-03-03"}).json()
    assert members["tickers"] == ["DOWN.US", "UP.US"]


def test_bad_index_import_is_422(client):
    resp = client.post(
        "/api/universes/index-history",
        json={"index_id": "toy", "format": "csv", "content": "nope"},
        headers=AUTH,
    )
    assert resp.status_code == 422


def test_delete(client):
    body = {"id": "gone", "kind": "list", "spec": {"tickers": ["UP.US"]}}
    client.post("/api/universes", json=body, headers=AUTH)
    resp = client.delete("/api/universes/gone", headers=AUTH)
    assert resp.status_code == 200
    assert client.get("/api/universes/gone").status_code == 404


def test_viewers_cannot_change_universes(settings, seeded, listing_source):
    from datetime import UTC, datetime

    from stonks.accounts import Role
    from tests.integration.auth.helpers import add_user, make_service, session_principal

    settings.api.allowed_hosts = ["testserver"]
    auth = make_service(settings.state.path, legacy=API_TOKEN, clock=lambda: datetime.now(UTC))
    app = create_app(settings, source_factory=lambda: listing_source)
    app.state.auth = auth
    viewer_id = add_user(settings.state.path, "viewer@example.com", role=Role.VIEWER)
    _, token = auth.create_token(
        session_principal(viewer_id, Role.VIEWER), name="v", scopes=["read"]
    )
    viewer = {"Authorization": f"Bearer {token}"}
    body = {"id": "mine", "kind": "list", "spec": {"tickers": ["UP.US"]}}
    with TestClient(app, client=LOOPBACK) as c:
        assert c.post("/api/universes", json=body, headers=viewer).status_code == 403
        assert c.post("/api/universes", json=body, headers=AUTH).status_code == 201
        assert c.get("/api/universes", headers=viewer).status_code == 200
        assert c.post("/api/universes/mine/refresh", headers=viewer).status_code == 403
        assert c.delete("/api/universes/mine", headers=viewer).status_code == 403
        change = {"kind": "list", "spec": {"tickers": ["DOWN.US"]}}
        assert c.put("/api/universes/mine", json=change, headers=viewer).status_code == 403
        assert c.get("/api/universes/mine/history", headers=viewer).status_code == 200
        assert c.get("/api/universes/exchanges", headers=viewer).status_code == 200


# ---- 20.10: edit, membership history and the exchange picker ------------------------


def test_update_replaces_the_definition_and_keeps_the_refresh(client):
    body = {"id": "edit", "kind": "list", "name": "Old", "spec": {"tickers": ["UP.US"]}}
    assert client.post("/api/universes", json=body, headers=AUTH).status_code == 201
    done = _wait(client, client.post("/api/universes/edit/refresh", headers=AUTH).json()["id"])
    assert done["status"] == "succeeded", done["error"]

    change = {"kind": "list", "name": "New", "spec": {"tickers": ["UP.US", "DOWN.US"]}}
    resp = client.put("/api/universes/edit", json=change, headers=AUTH)
    assert resp.status_code == 200, resp.text
    view = resp.json()
    assert view["name"] == "New" and view["spec"]["tickers"] == ["UP.US", "DOWN.US"]
    # The members stay as refreshed until the next refresh.
    assert view["member_count"] == 1 and view["refreshed_at"] is not None
    assert view["updated_at"] >= view["refreshed_at"]

    csv = {"kind": "list", "csv": "ticker\nFLAT.US\n"}
    updated = client.put("/api/universes/edit", json=csv, headers=AUTH).json()
    assert updated["spec"] == {"tickers": ["FLAT.US"]}


def test_update_errors(client):
    change = {"kind": "list", "spec": {"tickers": ["UP.US"]}}
    assert client.put("/api/universes/nope", json=change, headers=AUTH).status_code == 404
    body = {"id": "r2", "kind": "list", "spec": {"tickers": ["UP.US"]}}
    client.post("/api/universes", json=body, headers=AUTH)
    bad = {"kind": "rule", "spec": {"min_adv": 1}}  # no start
    assert client.put("/api/universes/r2", json=bad, headers=AUTH).status_code == 422
    csv_rule = {"kind": "rule", "csv": "ticker\nUP.US\n"}
    assert client.put("/api/universes/r2", json=csv_rule, headers=AUTH).status_code == 422
    assert client.put("/api/universes/r2", json=change).status_code == 401


def test_membership_history_lists_spans_latest_change_first(client):
    spec = {
        "spans": [
            {"ticker": "UP.US", "start_date": "2025-01-02"},
            {"ticker": "DOWN.US", "start_date": "2025-01-02", "end_date": "2025-06-02"},
            {"ticker": "DOWN.US", "start_date": "2025-09-01"},
        ],
        "tickers": ["FLAT.US"],
    }
    body = {"id": "spans", "kind": "list", "spec": spec}
    assert client.post("/api/universes", json=body, headers=AUTH).status_code == 201
    done = _wait(client, client.post("/api/universes/spans/refresh", headers=AUTH).json()["id"])
    assert done["status"] == "succeeded", done["error"]

    page = client.get("/api/universes/spans/history").json()
    assert page["total"] == 4
    assert page["items"] == [
        {"ticker": "DOWN.US", "start_date": "2025-09-01", "end_date": None},
        {"ticker": "DOWN.US", "start_date": "2025-01-02", "end_date": "2025-06-02"},
        {"ticker": "UP.US", "start_date": "2025-01-02", "end_date": None},
        # A plain ticker is a member from the start: no start date.
        {"ticker": "FLAT.US", "start_date": None, "end_date": None},
    ]
    found = client.get("/api/universes/spans/history", params={"ticker": "down", "limit": 1})
    assert found.json()["total"] == 2 and len(found.json()["items"]) == 1
    assert client.get("/api/universes/nope/history").status_code == 404


def test_exchanges_come_from_the_instruments_we_hold(client, listing_source):
    listing_source._listings = [
        SymbolListing(ticker="AAA.US", exchange="NYSE", security_type="common_stock"),
        SymbolListing(ticker="BBB.US", exchange="NYSE", security_type="common_stock"),
        SymbolListing(ticker="DEAD.US", exchange="NASDAQ", is_delisted=True),
    ]
    body = {"id": "us_all", "kind": "exchange", "spec": {"exchange": "US"}}
    assert client.post("/api/universes", json=body, headers=AUTH).status_code == 201
    done = _wait(client, client.post("/api/universes/us_all/refresh", headers=AUTH).json()["id"])
    assert done["status"] == "succeeded", done["error"]

    rows = client.get("/api/universes/exchanges").json()["items"]
    assert rows == [
        {"exchange": "NASDAQ", "instruments": 1, "listed": 0},
        {"exchange": "NYSE", "instruments": 2, "listed": 2},
    ]
