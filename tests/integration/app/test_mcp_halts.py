"""MCP kill-switch tools end to end (MCP client -> MCP server -> ApiClient
-> the FastAPI app): listing halts, and a guarded kill switch. Resuming
stays in the console and the CLI."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from stonks.api import create_app
from stonks.mcp.server import build_server
from tests.integration.app.test_api import AUTH
from tests.integration.app.test_mcp_server import _api, call, call_error


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def test_client(settings, seeded, fake_source):
    settings.api.allowed_hosts = ["testserver"]
    app = create_app(settings, source_factory=lambda: fake_source)
    with TestClient(app, client=("127.0.0.1", 50000)) as tc:
        yield tc


@pytest.fixture
async def mcp(test_client):
    async with Client(build_server(_api(test_client), max_wait_seconds=60)) as c:
        yield c


@pytest.mark.anyio
async def test_the_kill_switch_is_guarded_and_listed(mcp, test_client):
    assert (await call(mcp, "list_halts")) == {"items": []}
    preview = await call(mcp, "engage_kill_switch", {"scope": "user", "reason": "away"})
    assert preview["preview"] is True and preview["applied"] is False
    assert test_client.get("/api/halts", headers=AUTH).json() == []
    done = await call(
        mcp, "engage_kill_switch", {"scope": "user", "reason": "away", "confirm": True}
    )
    assert done["applied"] is True and done["halt"]["kind"] == "kill"
    listed = await call(mcp, "list_halts")
    assert [h["id"] for h in listed["items"]] == [done["halt"]["id"]]


@pytest.mark.anyio
async def test_a_portfolio_kill_switch_needs_its_id(mcp):
    err = await call_error(
        mcp, "engage_kill_switch", {"scope": "portfolio", "reason": "r", "confirm": True}
    )
    assert "portfolio_id" in err
