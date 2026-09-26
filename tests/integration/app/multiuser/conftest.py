"""Shared fixtures for multi-user API tests: four people with tokens
(Alice and Bob trade, Vic views, Ada is an admin) over one app."""

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
