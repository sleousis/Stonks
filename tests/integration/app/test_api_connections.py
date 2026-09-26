"""Broker-connection routes: providers, connect with keys, hosted portal,
link, sync, disconnect. Scoped to the bootstrap admin until S2; other users'
connections read as 404; credentials never come back in a response or a log."""

from __future__ import annotations

import logging
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from stonks.accounts import DEFAULT_OWNER_ID, Role, Scope, UserRepository
from stonks.api import create_app
from stonks.app.connections import ConnectionsAppService
from stonks.app.context import AppContext
from stonks.app.services import Services
from stonks.connections.providers import fake
from stonks.connections.ratelimit import reset_limiters
from stonks.connections.service import ConnectionService
from stonks.connections.settings import ConnectionsConfig
from stonks.security import KeyRing, SecretBox, generate_key
from stonks.store.state import SqliteState
from tests.integration.app.stepup import allow_step_up
from tests.integration.app.test_api import AUTH, LOOPBACK, REMOTE

TOKEN = "demo-secret-token-9f8e7d"
CONFIG = ConnectionsConfig(enabled_providers=("fake", "fake_portal"))


@pytest.fixture(autouse=True)
def _clean_fakes():
    fake.FAKE_BOOKS.clear()
    reset_limiters()
    yield
    fake.FAKE_BOOKS.clear()
    reset_limiters()


@pytest.fixture
def box() -> SecretBox:
    return SecretBox(KeyRing.parse(f"k1:{generate_key()}"))


@pytest.fixture
def services(settings, seeded, fake_source, box):
    settings.api.allowed_hosts = ["testserver"]
    ctx = AppContext(settings, source_factory=lambda: fake_source)
    svc = Services.create(ctx)
    svc.connections = ConnectionsAppService(ctx, config=CONFIG, box=box)
    return svc


@pytest.fixture
def client(settings, services):
    # Connecting and deleting need a fresh second factor (step-up).
    app = allow_step_up(create_app(settings, services=services))
    with TestClient(app, client=LOOPBACK) as c:
        yield c


def _connect(client: TestClient, token: str = TOKEN) -> dict:
    resp = client.post(
        "/api/connections/keys",
        json={"provider": "fake", "fields": {"token": token}, "label": "Demo"},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_scoped_routes_need_the_token_even_for_loopback_reads(client):
    # Personal data: no open-on-loopback exemption (there is no principal).
    assert client.get("/api/connections").status_code == 401
    assert client.get("/api/connections/providers").status_code == 401
    assert client.get("/api/connections", headers=AUTH).status_code == 200


def test_providers_lists_every_provider_and_marks_the_enabled_ones(client):
    resp = client.get("/api/connections/providers", headers=AUTH)
    assert resp.status_code == 200
    by_name = {p["name"]: p for p in resp.json()}
    assert set(by_name) == {"alpaca", "fake", "fake_portal", "snaptrade"}
    assert {n for n, p in by_name.items() if p["enabled"]} == {"fake", "fake_portal"}
    assert by_name["fake"]["auth_flow"] == "api_key"
    assert by_name["fake"]["credential_fields"] == ["token"]
    assert by_name["alpaca"]["has_paper"] is True
    assert by_name["snaptrade"]["has_paper"] is False


def test_a_disabled_provider_cannot_be_connected(client):
    resp = client.post(
        "/api/connections/keys",
        json={"provider": "alpaca", "fields": {"api_key": "a", "secret_key": "b"}},
        headers=AUTH,
    )
    assert resp.status_code in (403, 409, 422), resp.text


def test_connections_count_their_accounts(client):
    body = _connect(client)
    assert body["accounts_count"] == 1
    [listed] = client.get("/api/connections", headers=AUTH).json()
    assert listed["accounts_count"] == 1
    got = client.get(f"/api/connections/{body['id']}", headers=AUTH).json()
    assert got["accounts_count"] == 1


def test_connect_with_keys_never_echoes_or_logs_the_secret(client, settings, caplog):
    caplog.set_level(logging.DEBUG)
    body = _connect(client)
    assert body["provider"] == "fake"
    assert body["status"] == "active"
    assert TOKEN not in str(body)
    # Nothing that leaves the API holds the secret: not the list, not the
    # accounts, not the audit rows, not the logs.
    listed = client.get("/api/connections", headers=AUTH)
    assert TOKEN not in listed.text
    accounts = client.get(f"/api/connections/{body['id']}/accounts", headers=AUTH)
    assert accounts.status_code == 200
    assert TOKEN not in accounts.text
    [acc] = accounts.json()
    assert acc["portfolio_id"]  # a broker portfolio was created and linked
    audit = _audit(settings, body["id"])
    assert any(a == "connection.connect" for _, a, _ in audit)
    assert all(actor == f"user:{DEFAULT_OWNER_ID}" for actor, _, _ in audit)
    assert TOKEN not in str(audit)
    assert TOKEN not in caplog.text


def _audit(settings, target_id: str) -> list[tuple[str, str, str]]:
    with SqliteState(settings.state.path) as state:
        rows = state.sql(
            "SELECT actor, action, details_json FROM audit_log WHERE target_id = ? ORDER BY id",
            [target_id],
        )
    return [(r["actor"], r["action"], r["details_json"]) for r in rows]


def test_refused_keys_are_422_without_the_secret(client):
    bad = "bad-secret-value-123"
    resp = client.post(
        "/api/connections/keys",
        json={"provider": "fake", "fields": {"token": bad}},
        headers=AUTH,
    )
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert bad not in resp.text
    assert client.get("/api/connections", headers=AUTH).json() == []


def test_validation_errors_do_not_echo_credentials(client):
    secret = "x" * 5000
    resp = client.post(
        "/api/connections/keys",
        json={"provider": "fake", "fields": {"token": secret}, "extra": 1},
        headers=AUTH,
    )
    assert resp.status_code == 422
    assert secret not in resp.text


def test_disabled_provider_is_refused(client):
    resp = client.post(
        "/api/connections/keys",
        json={"provider": "alpaca", "fields": {"api_key": "a", "secret_key": "b"}},
        headers=AUTH,
    )
    assert resp.status_code == 422


def test_get_sync_and_delete_a_connection(client):
    conn = _connect(client)
    cid = conn["id"]
    got = client.get(f"/api/connections/{cid}", headers=AUTH)
    assert got.status_code == 200
    assert got.json()["id"] == cid

    synced = client.post(f"/api/connections/{cid}/sync", headers=AUTH)
    assert synced.status_code == 200, synced.text
    result = synced.json()
    assert result["status"] == "ok"
    [pf] = result["portfolios"]
    assert pf["positions"] == 3
    assert pf["unmapped"] == ["XYZ123"]

    deleted = client.delete(f"/api/connections/{cid}", headers=AUTH)
    assert deleted.status_code == 200
    assert deleted.json()["archived_portfolios"] == [pf["portfolio_id"]]
    assert client.get(f"/api/connections/{cid}", headers=AUTH).status_code == 404


def test_link_account_to_an_existing_broker_portfolio(client, settings):
    conn = _connect(client)
    cid = conn["id"]
    [acc] = client.get(f"/api/connections/{cid}/accounts", headers=AUTH).json()
    # Already linked to the auto-created portfolio: linking again elsewhere is refused.
    again = client.post(
        f"/api/connections/{cid}/link",
        json={"external_account_id": acc["external_account_id"]},
        headers=AUTH,
    )
    assert again.status_code == 422
    missing = client.post(
        f"/api/connections/{cid}/link",
        json={"external_account_id": "nope"},
        headers=AUTH,
    )
    assert missing.status_code == 404
    same = client.post(
        f"/api/connections/{cid}/link",
        json={
            "external_account_id": acc["external_account_id"],
            "portfolio_id": acc["portfolio_id"],
        },
        headers=AUTH,
    )
    assert same.status_code == 200, same.text
    assert same.json()["portfolio_id"] == acc["portfolio_id"]


def test_portal_flow_start_and_callback(client):
    start = client.post(
        "/api/connections/portal",
        json={"provider": "fake_portal", "redirect_uri": "http://localhost:4200/connections/done"},
        headers=AUTH,
    )
    assert start.status_code == 201, start.text
    link = start.json()
    assert link["url"].startswith("https://")
    cid = link["connection_id"]
    assert client.get(f"/api/connections/{cid}", headers=AUTH).json()["status"] == "pending"

    # The provider sends the browser back to the redirect with connection_id
    # and state; the console forwards both here.
    callback = parse_qs(urlsplit(fake_redirect(link["url"])).query)
    state = callback["state"][0]
    bad = client.get(
        "/api/connections/callback",
        params={"connection_id": cid, "state": "wrong"},
        headers=AUTH,
    )
    assert bad.status_code == 422
    done = client.get(
        "/api/connections/callback",
        params={"connection_id": cid, "state": state},
        headers=AUTH,
    )
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "active"


def fake_redirect(portal_url: str) -> str:
    """The fake portal URL carries the callback URL in its ``redirect`` query."""
    return parse_qs(urlsplit(portal_url).query)["redirect"][0]


def test_other_users_connections_are_404(client, settings, box):
    with SqliteState(settings.state.path) as state:
        bob = Scope.for_user(
            UserRepository(state).create(display_name="Bob", role=Role.TRADER, actor="t")
        )
        rec = ConnectionService(state, CONFIG, box=box).connect_with_keys(
            bob, "fake", {"token": "bob-token-12345"}
        )
    cid = rec.id
    assert client.get("/api/connections", headers=AUTH).json() == []
    for method, path, body in [
        ("GET", f"/api/connections/{cid}", None),
        ("GET", f"/api/connections/{cid}/accounts", None),
        ("POST", f"/api/connections/{cid}/sync", None),
        ("POST", f"/api/connections/{cid}/link", {"external_account_id": "fake-acc-1"}),
        ("DELETE", f"/api/connections/{cid}", None),
    ]:
        resp = client.request(method, path, json=body, headers=AUTH)
        assert resp.status_code == 404, (method, path, resp.text)
    resp = client.get(
        "/api/connections/callback", params={"connection_id": cid, "state": "x"}, headers=AUTH
    )
    assert resp.status_code == 404


def test_writes_need_the_token(settings, services):
    with TestClient(create_app(settings, services=services), client=REMOTE) as remote:
        resp = remote.post(
            "/api/connections/keys", json={"provider": "fake", "fields": {"token": TOKEN}}
        )
        assert resp.status_code == 401
        assert remote.post("/api/connections/c/sync").status_code == 401
        assert remote.delete("/api/connections/c").status_code == 401


def test_connect_and_delete_need_a_step_up(settings, services):
    # Without a fresh second factor (any API token) connecting is refused.
    with TestClient(create_app(settings, services=services), client=LOOPBACK) as c:
        resp = c.post(
            "/api/connections/keys",
            json={"provider": "fake", "fields": {"token": TOKEN}},
            headers=AUTH,
        )
        assert resp.status_code == 403 and "step_up_required" in resp.json()["detail"]
        assert c.delete("/api/connections/c", headers=AUTH).status_code == 403
