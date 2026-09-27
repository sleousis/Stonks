"""MCP portfolio and subscription tools end to end (MCP client -> MCP server
-> ApiClient -> the FastAPI app), as the bootstrap admin who owns
``pf_default``."""

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
async def test_portfolio_reads(mcp):
    portfolios = await call(mcp, "list_portfolios")
    assert [p["id"] for p in portfolios["items"]] == ["pf_default"]
    modes = await call(mcp, "list_trading_modes")
    assert modes["items"][0]["trading"] == "paper"


@pytest.mark.anyio
async def test_subscribe_is_guarded_then_applies(mcp, test_client):
    args = {"strategy_id": "bah_shadow", "mode": "paper", "portfolio_id": "pf_default"}
    # the default book already follows the active strategy
    before = test_client.get("/api/subscriptions", headers=AUTH).json()["items"]
    assert [s["strategy_id"] for s in before] == ["bah_active"]
    preview = await call(mcp, "subscribe", args)
    assert preview["preview"] is True and preview["applied"] is False
    assert test_client.get("/api/subscriptions", headers=AUTH).json()["items"] == before
    done = await call(mcp, "subscribe", args | {"confirm": True})
    sub = done["subscription"]
    assert done["applied"] is True and sub["mode"] == "paper"
    listed = await call(mcp, "list_subscriptions")
    assert {s["id"] for s in listed["items"]} == {before[0]["id"], sub["id"]}

    off = await call(mcp, "update_subscription", {"subscription_id": sub["id"], "enabled": False})
    assert off["preview"] is True
    items = test_client.get("/api/subscriptions", headers=AUTH).json()["items"]
    assert next(s for s in items if s["id"] == sub["id"])["enabled"] is True
    applied = await call(
        mcp,
        "update_subscription",
        {"subscription_id": sub["id"], "enabled": False, "confirm": True},
    )
    assert applied["subscription"]["enabled"] is False


@pytest.mark.anyio
async def test_auto_is_refused_with_a_pointer_to_the_web_app(mcp):
    message = await call_error(
        mcp, "update_subscription", {"subscription_id": "sub_x", "mode": "auto", "confirm": True}
    )
    assert "web app" in message
