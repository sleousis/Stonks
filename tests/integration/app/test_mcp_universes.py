"""MCP universe tools end to end (MCP client -> MCP server -> ApiClient ->
the FastAPI app). Mutating tools preview unless confirm=true."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from stonks.api import create_app
from stonks.mcp.server import build_server
from tests.integration.app.test_mcp_server import _api, call


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def test_client(settings, seeded, fake_source):
    settings.api.allowed_hosts = ["testserver"]
    app = create_app(settings, source_factory=lambda: fake_source, sse_poll_seconds=0.02)
    with TestClient(app, client=("127.0.0.1", 50000)) as tc:
        yield tc


@pytest.fixture
async def mcp(test_client):
    async with Client(build_server(_api(test_client), max_wait_seconds=60)) as c:
        yield c


@pytest.mark.anyio
async def test_create_needs_confirm_then_refresh_and_members(mcp, test_client):
    args = {"universe_id": "mine", "kind": "list", "spec": {"tickers": ["UP.US", "FLAT.US"]}}
    preview = await call(mcp, "create_universe", args)
    assert preview["preview"] is True and preview["applied"] is False
    assert (await call(mcp, "list_universes")) == {"items": []}

    created = await call(mcp, "create_universe", {**args, "confirm": True})
    assert created["applied"] is True and created["universe"]["id"] == "mine"

    preview = await call(mcp, "refresh_universe", {"universe_id": "mine"})
    assert preview["preview"] is True
    queued = await call(mcp, "refresh_universe", {"universe_id": "mine", "confirm": True})
    waited = await call(mcp, "wait_for_job", {"job_id": queued["job"]["id"]})
    assert waited["job"]["status"] == "succeeded", waited
    assert waited["result"]["members"] == 2

    shown = await call(mcp, "get_universe", {"universe_id": "mine"})
    assert shown["member_count"] == 2
    members = await call(
        mcp, "get_universe_members", {"universe_id": "mine", "as_of": "2026-01-02"}
    )
    assert members["tickers"] == ["FLAT.US", "UP.US"]


@pytest.mark.anyio
async def test_ensure_data_needs_confirm(mcp, test_client):
    await call(
        mcp,
        "create_universe",
        {"universe_id": "u", "kind": "list", "spec": {"tickers": ["NEW.US"]}, "confirm": True},
    )
    args = {"universe_id": "u", "start": "2026-04-01", "end": "2026-04-03"}
    preview = await call(mcp, "ensure_universe_data", args)
    assert preview["preview"] is True
    assert test_client.get("/api/jobs", params={"kind": "universe_ensure"}).json()["total"] == 0
    queued = await call(mcp, "ensure_universe_data", {**args, "confirm": True})
    assert queued["applied"] is True and queued["job"]["kind"] == "universe_ensure"


@pytest.mark.anyio
async def test_index_import_needs_confirm(mcp, test_client):
    args = {
        "index_id": "toy",
        "format": "csv",
        "content": "date,ticker,action\n2026-01-02,UP.US,member\n",
    }
    assert (await call(mcp, "import_index_history", args))["preview"] is True
    imported = await call(mcp, "import_index_history", {**args, "confirm": True})
    assert imported["result"]["constituents"] == 1


@pytest.mark.anyio
async def test_delete_needs_confirm(mcp, test_client):
    args = {"universe_id": "gone", "kind": "list", "spec": {"tickers": ["UP.US"]}}
    await call(mcp, "create_universe", {**args, "confirm": True})
    preview = await call(mcp, "delete_universe", {"universe_id": "gone"})
    assert preview["preview"] is True and preview["target"]["id"] == "gone"
    assert [u["id"] for u in (await call(mcp, "list_universes"))["items"]] == ["gone"]
    deleted = await call(mcp, "delete_universe", {"universe_id": "gone", "confirm": True})
    assert deleted["applied"] is True and deleted["universe"]["id"] == "gone"
    assert (await call(mcp, "list_universes")) == {"items": []}


def test_universe_and_lab_ensure_jobs_have_typed_result_routes():
    from stonks.mcp.tools.jobs import RESULT_ROUTES

    assert RESULT_ROUTES["universe_refresh"] == "/api/universes/refresh/{id}/result"
    assert RESULT_ROUTES["universe_ensure"] == "/api/universes/ensure/{id}/result"
    assert RESULT_ROUTES["lab_ensure"] == "/api/lab/ensure/{id}/result"
