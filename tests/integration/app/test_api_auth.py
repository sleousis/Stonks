"""Auth over HTTP: login with a mandatory second factor, cookies and CSRF,
personal API tokens and scopes, admin routes, the legacy token shim, and
tenant isolation across every user-scoped route."""

from __future__ import annotations

import base64
import re
from datetime import UTC, datetime

import pyotp
import pytest
from fastapi.testclient import TestClient

from stonks.accounts import DEFAULT_OWNER_ID, Role
from stonks.api import create_app
from stonks.app.connections import ConnectionsAppService, ConnectWithKeysRequest
from stonks.app.context import AppContext
from stonks.app.services import Services
from stonks.auth import AuthService
from stonks.connections.providers import fake
from stonks.connections.ratelimit import reset_limiters
from stonks.connections.settings import ConnectionsConfig
from stonks.security import KeyRing, SecretBox, generate_key
from stonks.store.state import SqliteState
from tests.integration.app.stepup import allow_step_up
from tests.integration.app.test_api import AUTH, LOOPBACK, REMOTE
from tests.integration.auth.helpers import PASSWORD, add_user, make_service, session_principal

BASE = "https://testserver"


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
def auth(settings, seeded, box) -> AuthService:
    return make_service(
        settings.state.path, legacy="test-token-123", box=box, clock=lambda: datetime.now(UTC)
    )


@pytest.fixture
def app(settings, seeded, fake_source, box, auth):
    settings.api.allowed_hosts = ["testserver"]
    ctx = AppContext(settings, source_factory=lambda: fake_source)
    svc = Services.create(ctx)
    svc.connections = ConnectionsAppService(
        ctx, config=ConnectionsConfig(enabled_providers=("fake",)), box=box
    )
    application = create_app(settings, services=svc)
    application.state.auth = auth
    return application


def _client(app, peer=REMOTE) -> TestClient:
    return TestClient(app, client=peer, base_url=BASE)


@pytest.fixture
def remote(app):
    with _client(app) as c:
        yield c


def _sign_in(client: TestClient, email: str, password: str = PASSWORD) -> dict:
    """Full browser sign-in with TOTP enrolment; returns csrf, secret, codes."""
    resp = client.post("/api/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["next_step"] == "enrol"
    csrf = {"X-CSRF-Token": body["csrf_token"]}
    start = client.post("/api/auth/mfa/enrol", headers=csrf)
    assert start.status_code == 200, start.text
    secret = start.json()["secret"]
    done = client.post(
        "/api/auth/mfa/enrol/confirm", json={"code": pyotp.TOTP(secret).now()}, headers=csrf
    )
    assert done.status_code == 200, done.text
    return {
        "csrf": {"X-CSRF-Token": done.json()["csrf_token"]},
        "secret": secret,
        "codes": done.json()["recovery_codes"],
    }


# ---- browser flow --------------------------------------------------------------


def test_login_sets_secure_httponly_cookies_and_needs_second_factor(remote, settings):
    add_user(settings.state.path, "alice@example.com")
    resp = remote.post("/api/auth/login", json={"email": "alice@example.com", "password": PASSWORD})
    assert resp.status_code == 200
    cookies = resp.headers.get_list("set-cookie")
    session_cookie = next(c for c in cookies if c.startswith("stonks_session="))
    assert "HttpOnly" in session_cookie and "Secure" in session_cookie
    assert "samesite=lax" in session_cookie.lower()
    csrf_cookie = next(c for c in cookies if c.startswith("stonks_csrf="))
    assert "HttpOnly" not in csrf_cookie
    # Password alone opens nothing.
    me = remote.get("/api/auth/me")
    assert me.status_code == 401 and "mfa_required" in me.json()["detail"]
    assert remote.get("/api/connections").status_code == 401


def test_full_sign_in_then_me_then_logout(remote, settings):
    uid = add_user(settings.state.path, "alice@example.com")
    s = _sign_in(remote, "alice@example.com")
    assert len(s["codes"]) == 10
    me = remote.get("/api/auth/me").json()
    assert me["user_id"] == uid and me["via"] == "session" and me["mfa_enrolled"]
    assert me["scopes"] == ["read", "trade", "lab"] and me["mfa_fresh"]
    # Unsafe methods need the CSRF header with the cookie.
    body = {"name": "script", "scopes": ["read"]}
    assert remote.post("/api/auth/tokens", json=body).status_code == 403
    assert remote.post("/api/auth/tokens", json=body, headers=s["csrf"]).status_code == 201
    assert remote.post("/api/auth/logout", headers=s["csrf"]).status_code == 204
    assert remote.get("/api/auth/me").status_code == 401


def test_second_login_verifies_with_code_or_recovery_code(app, settings):
    add_user(settings.state.path, "alice@example.com")
    with _client(app) as first:
        s = _sign_in(first, "alice@example.com")
    with _client(app) as c:
        body = c.post(
            "/api/auth/login", json={"email": "alice@example.com", "password": PASSWORD}
        ).json()
        assert body["next_step"] == "verify"
        csrf = {"X-CSRF-Token": body["csrf_token"]}
        bad = c.post("/api/auth/mfa/verify", json={"code": "000000"}, headers=csrf)
        assert bad.status_code == 401
        ok = c.post("/api/auth/mfa/verify", json={"recovery_code": s["codes"][0]}, headers=csrf)
        assert ok.status_code == 200 and ok.json()["recovery_codes_left"] == 9
        assert c.get("/api/auth/me").status_code == 200


def test_login_is_rate_limited(remote, settings):
    add_user(settings.state.path, "alice@example.com")
    for _ in range(5):
        r = remote.post("/api/auth/login", json={"email": "alice@example.com", "password": "x"})
        assert r.status_code == 401
    r = remote.post("/api/auth/login", json={"email": "alice@example.com", "password": PASSWORD})
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0


def test_login_errors_never_echo_the_password(remote):
    r = remote.post("/api/auth/login", json={"email": "a@b.c", "password": 12345678901234})
    assert r.status_code == 422 and "12345678901234" not in r.text


PROXY = ("172.31.250.5", 40000)


def _login_ips(settings) -> list[str]:
    with SqliteState(settings.state.path) as state:
        return [r["ip"] for r in state.sql("SELECT ip FROM login_attempts ORDER BY id")]


def test_forwarded_client_ip_is_used_only_behind_a_trusted_proxy(app, settings):
    """``stonks serve`` runs uvicorn with ``forwarded_allow_ips`` =
    ``[api].trusted_proxies``: behind Caddy the real client IP is limited
    and audited, and a spoofed X-Forwarded-For from anyone else is ignored."""
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    served = ProxyHeadersMiddleware(app, trusted_hosts=["172.31.250.0/24"])
    body = {"email": "nobody@example.com", "password": "wrong password"}
    with TestClient(served, client=PROXY, base_url=BASE) as via_proxy:
        for client_ip in ("198.51.100.1", "198.51.100.2"):
            resp = via_proxy.post(
                "/api/auth/login", json=body, headers={"X-Forwarded-For": client_ip}
            )
            assert resp.status_code == 401
    with TestClient(served, client=REMOTE, base_url=BASE) as direct:
        resp = direct.post("/api/auth/login", json=body, headers={"X-Forwarded-For": "1.2.3.4"})
        assert resp.status_code == 401
    assert _login_ips(settings) == ["198.51.100.1", "198.51.100.2", REMOTE[0]]


def test_one_client_behind_the_proxy_cannot_lock_out_others(app, settings):
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    add_user(settings.state.path, "alice@example.com")
    served = ProxyHeadersMiddleware(app, trusted_hosts=["172.31.250.0/24"])
    attacker = {"X-Forwarded-For": "203.0.113.66"}
    with TestClient(served, client=PROXY, base_url=BASE) as c:
        for i in range(5):
            c.post(
                "/api/auth/login",
                json={"email": f"victim{i}@example.com", "password": "guess guess"},
                headers=attacker,
            )
        blocked = c.post(
            "/api/auth/login",
            json={"email": "alice@example.com", "password": PASSWORD},
            headers=attacker,
        )
        assert blocked.status_code == 429
        ok = c.post(
            "/api/auth/login",
            json={"email": "alice@example.com", "password": PASSWORD},
            headers={"X-Forwarded-For": "198.51.100.7"},
        )
        assert ok.status_code == 200, ok.text


# ---- tokens and scopes ------------------------------------------------------------


def test_personal_token_works_as_bearer_and_respects_scopes(remote, settings):
    add_user(settings.state.path, "alice@example.com")
    s = _sign_in(remote, "alice@example.com")
    created = remote.post(
        "/api/auth/tokens", json={"name": "ro", "scopes": ["read"]}, headers=s["csrf"]
    ).json()
    token = created["token"]
    assert token.startswith("stk_")
    listed = remote.get("/api/auth/tokens").json()
    assert [t["id"] for t in listed] == [created["info"]["id"]]
    assert "token" not in listed[0]

    with TestClient(remote.app, client=REMOTE, base_url=BASE) as script:
        bearer = {"Authorization": f"Bearer {token}"}
        assert script.get("/api/strategies", headers=bearer).status_code == 200
        me = script.get("/api/auth/me", headers=bearer).json()
        assert me["via"] == "token" and me["scopes"] == ["read"]
        # Read-only token: every unsafe method is refused by the floor.
        denied = script.post("/api/strategies/bah_shadow/promote", headers=bearer)
        assert denied.status_code == 403
        # Tokens can't create tokens.
        assert (
            script.post(
                "/api/auth/tokens", json={"name": "x", "scopes": ["read"]}, headers=bearer
            ).status_code
            == 403
        )

    revoke = remote.delete(f"/api/auth/tokens/{created['info']['id']}", headers=s["csrf"])
    assert revoke.status_code == 204
    assert (
        remote.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    )


def test_scopes_beyond_role_are_refused(remote, settings):
    add_user(settings.state.path, "viewer@example.com", Role.VIEWER)
    s = _sign_in(remote, "viewer@example.com")
    r = remote.post("/api/auth/tokens", json={"name": "x", "scopes": ["trade"]}, headers=s["csrf"])
    assert r.status_code == 403


# ---- legacy token -------------------------------------------------------------------


def test_legacy_env_token_is_the_bootstrap_admin(remote):
    me = remote.get("/api/auth/me", headers=AUTH)
    assert me.status_code == 200
    body = me.json()
    assert body["user_id"] == DEFAULT_OWNER_ID and body["via"] == "legacy"
    assert body["role"] == "admin"
    assert remote.get("/api/auth/check", headers=AUTH).status_code == 200
    assert remote.get("/api/auth/check").status_code == 401


def test_legacy_token_cannot_do_step_up_actions(remote):
    r = remote.post(
        "/api/auth/users",
        json={
            "email": "x@example.com",
            "display_name": "X",
            "role": "trader",
            "password": PASSWORD,
        },
        headers=AUTH,
    )
    assert r.status_code == 403 and "step_up_required" in r.json()["detail"]


# ---- admin -------------------------------------------------------------------------


def test_admin_user_management_over_http(app, settings, auth):
    auth.bootstrap_admin("owner@example.com", PASSWORD)
    with _client(app) as admin:
        s = _sign_in(admin, "owner@example.com")
        created = admin.post(
            "/api/auth/users",
            json={
                "email": "bob@example.com",
                "display_name": "Bob",
                "role": "trader",
                "password": PASSWORD,
            },
            headers=s["csrf"],
        )
        assert created.status_code == 201, created.text
        bob_id = created.json()["id"]
        users = admin.get("/api/auth/users").json()
        assert {u["email"] for u in users} >= {"owner@example.com", "bob@example.com"}
        assert all(set(u) == set(users[0]) for u in users)  # identity only
        patched = admin.patch(
            f"/api/auth/users/{bob_id}", json={"role": "viewer"}, headers=s["csrf"]
        )
        assert patched.json()["role"] == "viewer"
        missing = admin.patch(
            "/api/auth/users/usr_nope", json={"role": "viewer"}, headers=s["csrf"]
        )
        assert missing.status_code == 404
    with _client(app) as bob:
        _sign_in(bob, "bob@example.com")
        assert bob.get("/api/auth/users").status_code == 403


# ---- tenant isolation ----------------------------------------------------------------


def _user_scoped_routes() -> list[tuple[str, str]]:
    """Every mounted route whose dependencies resolve the caller's principal
    or scope, so a new user-scoped route is covered without editing this test."""
    from fastapi.routing import APIRoute

    from stonks.api import routers
    from stonks.api.deps import current_principal, current_scope

    def walks(dependant) -> bool:
        return any(
            d.call in (current_principal, current_scope) or walks(d) for d in dependant.dependencies
        )

    mounted = [
        *routers.API_ROUTERS,
        *routers.TOKEN_ROUTERS,
        *routers.STREAM_ROUTERS,
        *routers.PUBLIC_ROUTERS,
    ]
    out = []
    for router in mounted:
        for route in router.routes:
            if isinstance(route, APIRoute) and walks(route.dependant):
                out.extend((m, route.path) for m in sorted(route.methods))
    return out


P256DH = base64.urlsafe_b64encode(b"\x04" + b"\x01" * 64).rstrip(b"=").decode()
AUTH_KEY = base64.urlsafe_b64encode(b"\x02" * 16).rstrip(b"=").decode()


def test_user_a_cannot_reach_user_b_resources_through_any_scoped_route(app, settings, auth):
    path = settings.state.path
    alice_id = add_user(path, "alice@example.com")
    bob_id = add_user(path, "bob@example.com")
    _, alice_token = auth.create_token(
        session_principal(alice_id, Role.TRADER), name="a", scopes=["read", "trade", "lab"]
    )
    bob_token_info, bob_token = auth.create_token(
        session_principal(bob_id, Role.TRADER), name="b", scopes=["read", "trade", "lab"]
    )
    alice = {"Authorization": f"Bearer {alice_token}"}
    bob = {"Authorization": f"Bearer {bob_token}"}

    # Step-up routes reach the ownership check (404), not the step-up refusal.
    allow_step_up(app)
    with _client(app) as c:
        # Bob's private things: a connection, a push device, a notification, a token.
        # Connecting needs a step-up (a session), so Bob's link is made in-process.
        conn = app.state.services.connections.connect_with_keys(
            session_principal(bob_id, Role.TRADER).scope,
            ConnectWithKeysRequest(
                provider="fake", fields={"token": "bob-secret-xyz"}, label="Bob"
            ),
        )
        bob_conn = conn.id
        with SqliteState(path) as state:
            state.execute(
                "INSERT INTO alerts (level, title, message, created_at, user_id, category)"
                " VALUES ('info', 'bob-only-alert', 'for bob', '2026-09-26T00:00:00+00:00', ?,"
                " 'system')",
                [bob_id],
            )
        assert c.get("/api/notifications", headers=bob).json()["unread_count"] == 1

        bob_markers = [bob_conn, bob_token_info.id, "bob-only-alert", "Bob", bob_id]
        ids = {
            "connection_id": bob_conn,
            "token_id": bob_token_info.id,
            "user_id": bob_id,
            "job": "tick",
        }
        routes = _user_scoped_routes()
        assert ("GET", "/api/connections/{connection_id}") in routes
        assert ("DELETE", "/api/auth/tokens/{token_id}") in routes
        for method, route in routes:
            url = re.sub(r"\{(\w+)\}", lambda m: ids.get(m.group(1), "x"), route)
            if method == "GET":
                resp = c.get(url, headers=alice)
                assert resp.status_code in (200, 403, 404, 422, 503), (route, resp.status_code)
                for marker in bob_markers:
                    if marker in url:
                        continue  # a 404 may echo the id Alice sent herself
                    assert marker not in resp.text, (route, marker)
            elif "{" in route and "{job}" not in route:
                resp = c.request(method, url, json={}, headers=alice)
                # Another user's id reads as missing, never as forbidden-but-there.
                assert resp.status_code in (403, 404, 422), (method, route, resp.status_code)
                if bob_conn not in url:
                    assert bob_conn not in resp.text

        assert c.get(f"/api/connections/{bob_conn}", headers=alice).status_code == 404
        assert c.delete(f"/api/connections/{bob_conn}", headers=alice).status_code == 404
        assert c.delete(f"/api/auth/tokens/{bob_token_info.id}", headers=alice).status_code == 404
        assert c.get("/api/connections", headers=alice).json() == []
        assert c.get("/api/notifications", headers=alice).json()["unread_count"] == 0

        # Bob's things are untouched.
        assert c.get(f"/api/connections/{bob_conn}", headers=bob).status_code == 200
        assert c.get("/api/auth/me", headers=bob).status_code == 200
        assert c.get("/api/notifications", headers=bob).json()["unread_count"] == 1


def _every_get_route() -> list[str]:
    """Every mounted GET route, whatever its dependencies (review finding
    AS-08: a route that forgot the principal is exactly the one that leaks)."""
    from fastapi.routing import APIRoute

    from stonks.api import routers

    mounted = [
        *routers.API_ROUTERS,
        *routers.TOKEN_ROUTERS,
        *routers.STREAM_ROUTERS,
        *routers.PUBLIC_ROUTERS,
    ]
    return sorted(
        {
            route.path
            for router in mounted
            for route in router.routes
            if isinstance(route, APIRoute) and "GET" in route.methods
        }
    )


def test_no_get_route_shows_another_users_data(app, settings, auth):
    """Seed Bob's private rows (a notification, a job, a draft, a portfolio
    with an order and a snapshot) and walk every GET route as Alice, with
    Bob's ids in the path and in ``portfolio_id``. None of Bob's markers may
    come back."""
    from stonks.accounts import PortfolioRepository
    from stonks.accounts.scope import Scope

    path = settings.state.path
    alice_id = add_user(path, "alice@example.com")
    bob_id = add_user(path, "bob@example.com")
    _, alice_token = auth.create_token(
        session_principal(alice_id, Role.TRADER), name="a", scopes=["read", "trade", "lab"]
    )
    alice = {"Authorization": f"Bearer {alice_token}"}
    services = app.state.services
    bob_job = services.runner.store.create("backtest", {"marker": "BOBJOBPARAM"}, owner_id=bob_id)
    with SqliteState(path) as state:
        bob_scope = Scope(user_id=bob_id, role=Role.TRADER)
        bob_pf = PortfolioRepository(state).create(bob_scope, name="BOBBOOK").id
        state.execute(
            "INSERT INTO alerts (level, title, message, created_at, user_id, category)"
            " VALUES ('info', 'BOBALERT', 'for bob', '2026-09-26T00:00:00+00:00', ?, 'system')",
            [bob_id],
        )
        state.execute(
            "INSERT INTO orders (client_id, ticker, side, quantity, order_type, status,"
            " created_at, updated_at, portfolio_id) VALUES ('bob-order-1', 'BOBSECRET.US',"
            " 'buy', 7, 'market', 'filled', '2026-03-20T00:00:00+00:00',"
            " '2026-03-20T00:00:00+00:00', ?)",
            [bob_pf],
        )
        state.execute(
            "INSERT INTO portfolio_snapshots (taken_at, cash, positions_json, total_value,"
            " portfolio_id) VALUES ('2026-03-20T00:00:00+00:00', 4242.42,"
            " '{\"BOBSECRET.US\": 7}', 9999.99, ?)",
            [bob_pf],
        )
        state.execute(
            "INSERT INTO strategy_drafts (id, name, kind, spec_json, status, created_at,"
            " updated_at, owner_id) VALUES ('draft_bob', 'BOBDRAFT', 'rule', '{}', 'draft',"
            " '2026-09-26', '2026-09-26', ?)",
            [bob_id],
        )
    markers = ["BOBALERT", "BOBSECRET", "BOBBOOK", "BOBDRAFT", "BOBJOBPARAM", bob_job.id]
    ids = {"job_id": bob_job.id, "draft_id": "draft_bob", "portfolio_id": bob_pf}
    routes = _every_get_route()
    assert "/api/alerts" in routes and "/api/jobs/{job_id}" in routes
    with _client(app) as c:
        for route in routes:
            url = re.sub(r"\{(\w+)\}", lambda m: ids.get(m.group(1), "x"), route)
            for params in ({}, {"portfolio_id": bob_pf}):
                resp = c.get(url, params=params, headers=alice)
                assert resp.status_code in (200, 403, 404, 409, 422, 503), (
                    route,
                    resp.status_code,
                )
                for marker in markers:
                    if marker in url:
                        continue  # a 404 may echo the id Alice sent herself
                    assert marker not in resp.text, (route, params, marker)


def test_open_reads_on_loopback_still_work_without_credentials(app):
    with _client(app, peer=LOOPBACK) as c:
        assert c.get("/api/strategies").status_code == 200
        assert c.get("/api/auth/me").status_code == 401
