"""Fix waves for the service-surface review (roadmap 18.2): each test names
the finding it pins (AS-xx)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from stonks.accounts import Role
from stonks.api import create_app
from stonks.app.context import AppContext
from stonks.app.services import Services
from stonks.auth import AuthService
from stonks.security import KeyRing, SecretBox, generate_key
from stonks.store.state import SqliteState
from tests.integration.app.test_api import REMOTE
from tests.integration.auth.helpers import add_user, make_service, session_principal

BASE = "https://testserver"


@pytest.fixture
def box() -> SecretBox:
    return SecretBox(KeyRing.parse(f"k1:{generate_key()}"))


@pytest.fixture
def auth(settings, seeded, box) -> AuthService:
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


@pytest.fixture
def client(app):
    with TestClient(app, client=REMOTE, base_url=BASE) as c:
        yield c


def bearer(auth: AuthService, user_id: str, role: Role, scopes: list[str]) -> dict[str, str]:
    _, token = auth.create_token(session_principal(user_id, role), name="t", scopes=scopes)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def people(settings, auth) -> dict[str, dict]:
    """Alice (trader), Bob (trader), Vic (viewer) and Ada (admin), with full tokens."""
    path = settings.state.path
    out: dict[str, dict] = {}
    for name, role, scopes in [
        ("alice", Role.TRADER, ["read", "trade", "lab"]),
        ("bob", Role.TRADER, ["read", "trade", "lab"]),
        ("vic", Role.VIEWER, ["read"]),
        ("ada", Role.ADMIN, ["read", "trade", "lab", "admin"]),
    ]:
        uid = add_user(path, f"{name}@example.com", role)
        out[name] = {"id": uid, "headers": bearer(auth, uid, role, scopes), "role": role}
    return out


def _insert_alert(path, title: str, user_id: str | None) -> None:
    with SqliteState(path) as state:
        state.execute(
            "INSERT INTO alerts (level, title, message, created_at, user_id, category)"
            " VALUES ('info', ?, 'm', '2026-09-26T00:00:00+00:00', ?, 'system')",
            [title, user_id],
        )


# ---- AS-01 alerts ------------------------------------------------------------------


def test_as01_alerts_show_only_the_callers_notifications(client, settings, people):
    _insert_alert(settings.state.path, "bob-only-alert", people["bob"]["id"])
    _insert_alert(settings.state.path, "alice-alert", people["alice"]["id"])
    _insert_alert(settings.state.path, "admins-audience", None)

    vic = client.get("/api/alerts", headers=people["vic"]["headers"])
    assert vic.status_code == 200
    assert vic.json()["total"] == 0 and "bob-only-alert" not in vic.text

    alice = client.get("/api/alerts", headers=people["alice"]["headers"]).json()
    assert [a["title"] for a in alice["items"]] == ["alice-alert"]

    # Admins also see the admin audience (user_id NULL), never another user's rows.
    ada = client.get("/api/alerts", headers=people["ada"]["headers"]).json()
    assert [a["title"] for a in ada["items"]] == ["admins-audience"]


def test_as01_alerts_need_a_credential_even_on_loopback(settings, seeded, fake_source):
    settings.api.allowed_hosts = ["testserver"]
    app = create_app(settings, source_factory=lambda: fake_source)
    with TestClient(app, client=("127.0.0.1", 5000)) as c:
        assert c.get("/api/alerts").status_code == 401
