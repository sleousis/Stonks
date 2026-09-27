"""MCP screener and calendar tools end to end (MCP client -> MCP server ->
ApiClient -> the FastAPI app). Storing a universe and deleting a screen
preview unless confirm=true."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from stonks.api import create_app
from stonks.mcp.server import build_server
from stonks.store.lake import DuckDBLake
from tests.fixtures.screener import END, seed_market
from tests.integration.app.test_mcp_server import _api, call, call_error

AS_OF = END.isoformat()


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def test_client(settings, seeded, fake_source):
    with DuckDBLake(settings.lake.path) as lake:
        seed_market(lake)
    app = create_app(settings, source_factory=lambda: fake_source, sse_poll_seconds=0.02)
    with TestClient(app, client=("127.0.0.1", 50000)) as tc:
        yield tc


@pytest.fixture
async def mcp(test_client):
    async with Client(build_server(_api(test_client), max_wait_seconds=60)) as c:
        yield c


@pytest.mark.anyio
async def test_screen_save_run_and_delete(mcp):
    metrics = await call(mcp, "list_screen_metrics")
    assert "pe_ratio" in {m["id"] for m in metrics["items"]}
    spec = {"sectors": ["Tech"], "sort_by": "price"}
    ran = await call(mcp, "run_screen", {"spec": spec, "as_of": AS_OF})
    assert [r["ticker"] for r in ran["rows"]] == ["BBB.US", "AAA.US"]
    saved = await call(mcp, "create_screen", {"name": "Tech", "spec": spec})
    sid = saved["id"]
    assert [s["id"] for s in (await call(mcp, "list_screens"))["items"]] == [sid]
    changed = await call(mcp, "update_screen", {"screen_id": sid, "spec": {"limit": 1}})
    assert changed["spec"]["limit"] == 1 and changed["spec"]["sort_by"] is None
    assert (await call(mcp, "get_screen", {"screen_id": sid}))["spec"]["limit"] == 1
    ran = await call(mcp, "run_screen", {"screen_id": sid, "as_of": AS_OF})
    assert len(ran["rows"]) == 1
    preview = await call(mcp, "delete_screen", {"screen_id": sid})
    assert preview["preview"] is True and preview["target"]["name"] == "Tech"
    done = await call(mcp, "delete_screen", {"screen_id": sid, "confirm": True})
    assert done["applied"] is True
    assert "not found" in (await call_error(mcp, "get_screen", {"screen_id": sid})).lower()


@pytest.mark.anyio
async def test_save_screen_as_universe_needs_confirm(mcp):
    args = {
        "universe_id": "rising",
        "spec": {"filters": [{"metric": "return_3m", "min": 0.01}]},
        "start": "2024-06-03",
        "end": AS_OF,
    }
    preview = await call(mcp, "save_screen_as_universe", args)
    assert preview["preview"] is True
    assert (await call(mcp, "list_universes"))["items"] == []
    snap = await call(mcp, "save_screen_as_universe", {**args, "mode": "snapshot"})
    assert any("survivorship" in w for w in snap["warnings"])
    saved = await call(mcp, "save_screen_as_universe", {**args, "confirm": True})
    assert saved["applied"] is True and saved["universe"]["kind"] == "rule"
    waited = await call(mcp, "wait_for_job", {"job_id": saved["refresh_job"]["id"]})
    assert waited["job"]["status"] == "succeeded", waited
    members = await call(mcp, "get_universe_members", {"universe_id": "rising", "as_of": AS_OF})
    assert members["tickers"] == ["AAA.US"]


@pytest.mark.anyio
async def test_calendar_tools(mcp):
    cal = await call(mcp, "get_calendar", {"scope": "tickers", "tickers": ["AAA.US"]})
    assert cal["tickers"] == ["AAA.US"] and cal["earnings"] == []
    news = await call(mcp, "get_news", {"scope": "tickers", "tickers": ["AAA.US"]})
    assert news["items"] == []
    warn = await call(mcp, "get_earnings_warnings", {"tickers": ["AAA.US"]})
    assert warn["warnings"] == []
    kinds = await call(mcp, "list_event_alert_kinds")
    assert {k["kind"] for k in kinds["items"]} == {"earnings_upcoming", "ex_dividend_upcoming"}
    refused = await call_error(mcp, "get_news", {"scope": "all"})
    assert "tickers" in refused
