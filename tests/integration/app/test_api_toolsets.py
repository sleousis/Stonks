"""MCP toolsets on API tokens (roadmap 23.8): stored on the token, shown by
``/api/auth/me``, listed by ``/api/auth/toolsets``, and the assistant bridge
builds only the allowed tools."""

from __future__ import annotations

from datetime import UTC, datetime

import anyio
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from stonks.accounts import Role
from stonks.api import create_app
from stonks.api.routers.auth import TokenCreateRequest
from stonks.app.context import AppContext
from stonks.app.services import Services
from stonks.assistant.tools import McpToolBridge
from stonks.security import KeyRing, SecretBox, generate_key
from tests.integration.app.test_api import REMOTE
from tests.integration.auth.helpers import add_user, make_service, session_principal

BASE = "https://testserver"


@pytest.fixture
def auth(settings, seeded):
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


def test_a_limited_token_reports_its_toolsets(app, auth, settings):
    uid = add_user(settings.state.path, "alice@example.com")
    principal = session_principal(uid, Role.TRADER)
    info, token = auth.create_token(principal, name="mcp", scopes=["read"], toolsets=["risk"])
    assert info.toolsets == ("risk",)
    _, open_token = auth.create_token(principal, name="all", scopes=["read"])
    with TestClient(app, client=REMOTE, base_url=BASE) as c:
        me = c.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
        me_all = c.get("/api/auth/me", headers={"Authorization": f"Bearer {open_token}"}).json()
        tokens = c.get("/api/auth/tokens", headers={"Authorization": f"Bearer {token}"}).json()
        groups = c.get("/api/auth/toolsets", headers={"Authorization": f"Bearer {token}"}).json()
    assert me["toolsets"] == ["risk"] and me_all["toolsets"] is None
    assert {t["name"]: t["toolsets"] for t in tokens["items"]} == {"mcp": ["risk"], "all": None}
    names = [g["name"] for g in groups["items"]]
    assert "decisions" in names and "orders" in names


def test_unknown_or_empty_toolsets_are_refused(auth, settings):
    with pytest.raises(ValidationError, match="unknown MCP toolsets: nope"):
        TokenCreateRequest(name="x", scopes=["read"], toolsets=["risk", "nope"])
    with pytest.raises(ValidationError):
        TokenCreateRequest(name="x", scopes=["read"], toolsets=[])
    body = TokenCreateRequest(name="x", scopes=["read"], toolsets=["risk", "jobs", "risk"])
    assert body.toolsets == ["jobs", "risk"]
    from stonks.app.errors import ValidationError as AuthValidationError

    uid = add_user(settings.state.path, "bob@example.com")
    with pytest.raises(AuthValidationError):
        auth.create_token(
            session_principal(uid, Role.TRADER), name="x", scopes=["read"], toolsets=[]
        )


def test_the_assistant_bridge_offers_only_the_allowed_tools(app, settings):
    uid = add_user(settings.state.path, "carol@example.com")
    limited = session_principal(uid, Role.TRADER)
    from dataclasses import replace

    limited = replace(limited, toolsets=frozenset({"decisions"}))

    async def names():
        bridge = McpToolBridge(app, limited)
        try:
            return {t.name for t in await bridge.tools()}
        finally:
            await bridge.aclose()

    assert anyio.run(names) == {"whoami", "list_trade_decisions"}
