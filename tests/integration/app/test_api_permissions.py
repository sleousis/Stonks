"""Every unsafe route declares the permission it needs, and the policy is
enforced over HTTP: traders and lab tokens can't reach admin routes, and
the audit actor comes from the caller, never from the request body."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from stonks.accounts import DEFAULT_OWNER_ID, Role
from stonks.api import create_app
from stonks.api.deps import permission_of
from stonks.app.context import AppContext
from stonks.app.services import Services
from stonks.auth import AuthService
from stonks.security import KeyRing, SecretBox, generate_key
from stonks.store.state import SqliteState
from tests.integration.app.test_api import REMOTE
from tests.integration.auth.helpers import add_user, make_service, session_principal

BASE = "https://testserver"
_SAFE = {"GET", "HEAD", "OPTIONS"}

#: Sign-in routes check their own credentials (password, pending session).
PUBLIC_UNSAFE = {
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/logout"),
    ("POST", "/api/auth/mfa/enrol"),
    ("POST", "/api/auth/mfa/enrol/confirm"),
    ("POST", "/api/auth/mfa/verify"),
}


def _api_routes(routes) -> list[APIRoute]:
    """Every APIRoute of the app, through included routers (FastAPI keeps
    them nested as ``_IncludedRouter`` objects)."""
    out: list[APIRoute] = []
    for route in routes:
        if isinstance(route, APIRoute):
            out.append(route)
        elif hasattr(route, "original_router"):
            out.extend(_api_routes(route.original_router.routes))
    return out


def _permissions(dependant) -> list:
    found = []
    for dep in dependant.dependencies:
        perm = permission_of(dep.call)
        if perm is not None:
            found.append(perm)
        found.extend(_permissions(dep))
    return found


@pytest.fixture
def auth(settings, seeded) -> AuthService:
    box = SecretBox(KeyRing.parse(f"k1:{generate_key()}"))
    return make_service(
        settings.state.path, legacy="test-token-123", box=box, clock=lambda: datetime.now(UTC)
    )


@pytest.fixture
def app(settings, seeded, fake_source, auth):
    settings.api.allowed_hosts = ["testserver"]
    svc = Services.create(AppContext(settings, source_factory=lambda: fake_source))
    application = create_app(settings, services=svc)
    application.state.auth = auth
    return application


def test_every_unsafe_route_declares_a_permission(app):
    missing = []
    seen = 0
    for route in _api_routes(app.routes):
        for method in sorted(route.methods - _SAFE):
            seen += 1
            if (method, route.path) in PUBLIC_UNSAFE:
                continue
            if not _permissions(route.dependant):
                missing.append((method, route.path))
    assert seen > 30
    assert missing == []


def test_openapi_lists_the_permission_per_route(app):
    paths = app.openapi()["paths"]
    assert paths["/api/strategies/{strategy_id}/promote"]["post"]["x-permission"] == (
        "strategy.promote"
    )
    assert paths["/api/ticks"]["post"]["x-permission"] == "operations.run"
    # AS-15: a backup is an operation.
    assert paths["/api/backups"]["post"]["x-permission"] == "operations.run"
    assert "x-permission" not in paths["/api/auth/login"]["post"]


def test_public_allowlist_is_not_stale(app):
    unsafe = {(m, r.path) for r in _api_routes(app.routes) for m in r.methods - _SAFE}
    assert unsafe >= PUBLIC_UNSAFE


def _token(auth, user_id: str, role: Role, scopes: list[str]) -> dict:
    _, token = auth.create_token(session_principal(user_id, role), name="t", scopes=scopes)
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize(
    ("method", "url", "body"),
    [
        ("POST", "/api/strategies/bah_shadow/promote", {"override": True, "reason": "x" * 30}),
        ("POST", "/api/strategies/bah_active/retire", {"reason": "because"}),
        ("POST", "/api/strategies/bah_active/shadow", {"reason": "because"}),
        ("POST", "/api/ticks", {"dry_run": True}),
        ("POST", "/api/ingest/runs", {"tickers": ["NEW.US"]}),
        ("POST", "/api/schedule/tick/run-now", {}),
        ("POST", "/api/studio/drafts/d_x/enable", {}),
        ("POST", "/api/studio/drafts/d_x/register", None),
    ],
)
def test_traders_cannot_call_admin_routes(app, settings, auth, method, url, body):
    uid = add_user(settings.state.path, "trader@example.com", Role.TRADER)
    headers = _token(auth, uid, Role.TRADER, ["read", "trade", "lab"])
    with TestClient(app, client=REMOTE, base_url=BASE) as c:
        resp = c.request(method, url, json=body, headers=headers)
    assert resp.status_code == 403, resp.text


def test_lab_only_token_cannot_trade_or_run_ticks(app, settings, auth):
    uid = add_user(settings.state.path, "lab@example.com", Role.TRADER)
    headers = _token(auth, uid, Role.TRADER, ["lab"])
    with TestClient(app, client=REMOTE, base_url=BASE) as c:
        assert c.post("/api/ticks", json={"dry_run": True}, headers=headers).status_code == 403
        kill = c.post("/api/halts/kill", json={"scope": "user", "reason": "stop"}, headers=headers)
        assert kill.status_code == 403


def test_admin_token_may_promote_and_actor_is_the_caller(app, settings, auth):
    headers = _token(auth, DEFAULT_OWNER_ID, Role.ADMIN, ["read", "admin"])
    with TestClient(app, client=REMOTE, base_url=BASE) as c:
        resp = c.post(
            "/api/strategies/bah_active/shadow",
            json={"reason": "demote for test", "actor": "someone-else"},
            headers=headers,
        )
    assert resp.status_code == 200, resp.text
    with SqliteState(settings.state.path) as state:
        rows = state.sql(
            "SELECT actor FROM status_changes WHERE strategy_id = 'bah_active'"
            " ORDER BY id DESC LIMIT 1"
        )
    assert rows[0]["actor"] == f"user:{DEFAULT_OWNER_ID}"
