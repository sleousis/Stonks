"""MCP tools end to end: MCP client -> MCP server -> ApiClient -> the real
FastAPI app (TestClient bridged through an httpx2 MockTransport)."""

from __future__ import annotations

import json
import logging
from typing import Any

import anyio
import httpx2
import pytest
from fastapi.testclient import TestClient
from mcp import Client

from stonks.api import create_app
from stonks.mcp.client import ApiClient
from stonks.mcp.server import build_server
from tests.integration.app.conftest import API_TOKEN

BAH = "stonks.strategies.examples.buy_and_hold:BuyAndHold"
BASE = "http://127.0.0.1:8000"
_HOP_HEADERS = {"content-length", "content-encoding", "transfer-encoding"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


def bridge(tc: TestClient) -> httpx2.MockTransport:
    """Forward httpx2 requests to a Starlette TestClient off the event loop."""

    async def handler(request: httpx2.Request) -> httpx2.Response:
        def forward() -> httpx2.Response:
            headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_HEADERS}
            target = request.url.raw_path.decode()
            resp = tc.request(request.method, target, headers=headers, content=request.content)
            out = [
                (k, v)
                for k, v in resp.headers.items()
                if k.lower() not in _HOP_HEADERS and k.lower() != "host"
            ]
            return httpx2.Response(resp.status_code, headers=out, content=resp.content)

        return await anyio.to_thread.run_sync(forward)

    return httpx2.MockTransport(handler)


@pytest.fixture
def test_client(settings, seeded, fake_source):
    app = create_app(settings, source_factory=lambda: fake_source, sse_poll_seconds=0.02)
    with TestClient(app, client=("127.0.0.1", 50000)) as tc:
        yield tc


def _api(tc: TestClient, token: str | None = API_TOKEN) -> ApiClient:
    return ApiClient(BASE, token=token, transport=bridge(tc))


@pytest.fixture
async def mcp(test_client):
    async with Client(build_server(_api(test_client), max_wait_seconds=120)) as c:
        yield c


async def call(client: Client, name: str, args: dict[str, Any] | None = None) -> Any:
    result = await client.call_tool(name, args or {})
    assert not result.is_error, result.content[0].text
    return result.structured_content


async def call_error(client: Client, name: str, args: dict[str, Any] | None = None) -> str:
    result = await client.call_tool(name, args or {})
    assert result.is_error, result.structured_content
    return result.content[0].text


# ---- tool catalogue -------------------------------------------------------------

READ_TOOLS = {
    "health",
    "get_portfolio",
    "list_portfolio_snapshots",
    "list_strategies",
    "get_strategy",
    "search_instruments",
    "get_bars",
    "get_coverage",
    "list_orders",
    "list_fills",
    "list_ticks",
    "get_tick",
    "list_ingest_runs",
    "get_catalog",
    "list_jobs",
    "get_job",
    "wait_for_job",
}
JOB_TOOLS = {"run_backtest", "run_lab", "run_ingest"}
GUARDED_TOOLS = {"promote_strategy", "retire_strategy", "shadow_strategy", "run_tick"}


@pytest.mark.anyio
async def test_tool_list_and_annotations(mcp):
    tools = {t.name: t for t in (await mcp.list_tools()).tools}
    assert set(tools) == READ_TOOLS | JOB_TOOLS | GUARDED_TOOLS
    for name, tool in tools.items():
        assert tool.description, name
        ann = tool.annotations
        assert ann is not None, name
        if name in READ_TOOLS:
            assert ann.read_only_hint is True and ann.destructive_hint is False, name
        else:
            assert ann.read_only_hint is False, name
        if name in JOB_TOOLS:
            assert ann.destructive_hint is False, name
        if name in GUARDED_TOOLS:
            assert ann.destructive_hint is True, name
            props = tool.input_schema["properties"]
            assert props["confirm"]["default"] is False, name
    assert tools["run_tick"].input_schema["properties"]["dry_run"]["default"] is True


@pytest.mark.anyio
async def test_no_tool_touches_broker_settings(mcp):
    names = {t.name for t in (await mcp.list_tools()).tools}
    assert not any("broker" in n or "live" in n or "config" in n for n in names)


# ---- read tools -----------------------------------------------------------------


@pytest.mark.anyio
async def test_health_and_portfolio(mcp):
    assert (await call(mcp, "health"))["status"] == "ok"
    portfolio = await call(mcp, "get_portfolio")
    assert portfolio["total_value"] > 0
    assert [p["ticker"] for p in portfolio["positions"]] == ["UP.US"]
    snaps = await call(mcp, "list_portfolio_snapshots", {"limit": 5})
    assert snaps["total"] == 1


@pytest.mark.anyio
async def test_strategies(mcp, seeded):
    listed = await call(mcp, "list_strategies", {"status": "active"})
    assert [s["id"] for s in listed["items"]] == [seeded["active_id"]]
    detail = await call(mcp, "get_strategy", {"strategy_id": seeded["active_id"]})
    assert detail["survival_reports"][0]["test_id"] == "oos"
    err = await call_error(mcp, "get_strategy", {"strategy_id": "nope"})
    assert "nope" in err


@pytest.mark.anyio
async def test_market(mcp):
    found = await call(mcp, "search_instruments", {"q": "Up"})
    assert [i["id"] for i in found["items"]] == ["UP.US"]
    bars = await call(
        mcp, "get_bars", {"ticker": "UP.US", "start": "2026-03-01", "end": "2026-03-06"}
    )
    assert len(bars["bars"]) == 5
    coverage = await call(mcp, "get_coverage", {"ticker": "UP.US"})
    assert coverage["items"][0]["ticker"] == "UP.US"


@pytest.mark.anyio
async def test_orders_fills_ticks(mcp, seeded):
    orders = await call(mcp, "list_orders", {"tick_id": seeded["tick_id"]})
    assert orders["total"] >= 1
    fills = await call(mcp, "list_fills", {"tick_id": seeded["tick_id"]})
    assert fills["total"] >= 1
    ticks = await call(mcp, "list_ticks")
    assert ticks["items"][0]["id"] == seeded["tick_id"]
    tick = await call(mcp, "get_tick", {"tick_id": seeded["tick_id"]})
    assert tick["orders"]


@pytest.mark.anyio
async def test_catalog_ingest_runs_and_jobs(mcp):
    catalog = await call(mcp, "get_catalog")
    assert any(c["class_path"] == BAH and c["parameters"] for c in catalog["strategies"])
    assert catalog["intervals"] and catalog["asset_classes"]
    assert (await call(mcp, "list_ingest_runs"))["total"] == 0
    assert (await call(mcp, "list_jobs"))["total"] == 0


# ---- job tools ------------------------------------------------------------------


@pytest.mark.anyio
async def test_backtest_job_and_wait(mcp):
    job = await call(
        mcp,
        "run_backtest",
        {
            "class_path": BAH,
            "params": {"ticker": "UP.US"},
            "universe": ["UP.US"],
            "start": "2025-10-01",
            "end": "2026-04-01",
        },
    )
    assert job["kind"] == "backtest"
    done = await call(mcp, "wait_for_job", {"job_id": job["id"], "poll_seconds": 0.05})
    assert done["timed_out"] is False
    assert done["job"]["status"] == "succeeded", done["job"]["error"]
    assert done["job"]["result"]["final_return"] > 0.5
    assert (await call(mcp, "get_job", {"job_id": job["id"]}))["status"] == "succeeded"
    assert (await call(mcp, "list_jobs", {"kind": "backtest"}))["total"] == 1


@pytest.mark.anyio
async def test_backtest_needs_exactly_one_strategy_source(mcp):
    args = {"universe": ["UP.US"], "start": "2025-10-01", "end": "2026-04-01"}
    assert "exactly one" in await call_error(mcp, "run_backtest", args)
    both = {**args, "class_path": BAH, "strategy_id": "x"}
    assert "exactly one" in await call_error(mcp, "run_backtest", both)


@pytest.mark.anyio
async def test_backtest_validation_error_is_readable(mcp):
    err = await call_error(
        mcp,
        "run_backtest",
        {"class_path": BAH, "universe": ["UP.US"], "start": "2026-05-01", "end": "2026-01-01"},
    )
    assert "422" in err


@pytest.mark.anyio
async def test_lab_job(mcp):
    job = await call(
        mcp,
        "run_lab",
        {
            "class_path": "stonks.strategies.examples.momentum:Momentum",
            "universe": ["UP.US", "DOWN.US"],
            "start": "2025-10-01",
            "end": "2026-04-01",
            "budget": 2,
            "survival_tests": ["oos"],
        },
    )
    done = await call(mcp, "wait_for_job", {"job_id": job["id"], "poll_seconds": 0.05})
    assert done["job"]["status"] == "succeeded", done["job"]["error"]
    assert done["job"]["result"]["verdict"] in ("pass", "fail")


@pytest.mark.anyio
async def test_ingest_job(mcp):
    job = await call(mcp, "run_ingest", {"kind": "prices", "tickers": ["NEW.US"]})
    done = await call(mcp, "wait_for_job", {"job_id": job["id"], "poll_seconds": 0.05})
    assert done["job"]["status"] == "succeeded", done["job"]["error"]
    assert (await call(mcp, "list_ingest_runs"))["total"] == 1


# ---- guarded tools ----------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tool", "target"),
    [("promote_strategy", "active"), ("retire_strategy", "retired")],
)
async def test_status_change_previews_without_confirm(mcp, seeded, tool, target):
    sid = seeded["shadow_id"]
    preview = await call(mcp, tool, {"strategy_id": sid})
    assert preview["preview"] is True and preview["applied"] is False
    assert preview["current_status"] == "shadow"
    assert preview["new_status"] == target
    assert (await call(mcp, "get_strategy", {"strategy_id": sid}))["status"] == "shadow"

    applied = await call(mcp, tool, {"strategy_id": sid, "confirm": True})
    assert applied["applied"] is True
    assert applied["previous_status"] == "shadow"
    assert applied["strategy"]["status"] == target


@pytest.mark.anyio
async def test_shadow_strategy_guarded(mcp, seeded):
    sid = seeded["active_id"]
    preview = await call(mcp, "shadow_strategy", {"strategy_id": sid})
    assert preview["new_status"] == "shadow"
    assert (await call(mcp, "get_strategy", {"strategy_id": sid}))["status"] == "active"
    await call(mcp, "shadow_strategy", {"strategy_id": sid, "confirm": True})
    assert (await call(mcp, "get_strategy", {"strategy_id": sid}))["status"] == "shadow"


@pytest.mark.anyio
async def test_run_tick_previews_without_confirm(mcp, seeded):
    preview = await call(mcp, "run_tick", {"tickers": ["UP.US"]})
    assert preview["preview"] is True
    assert preview["request"]["dry_run"] is True
    assert preview["active_strategies"] == [seeded["active_id"]]
    assert (await call(mcp, "list_jobs"))["total"] == 0


@pytest.mark.anyio
async def test_run_tick_confirmed_defaults_to_dry_run(mcp):
    out = await call(mcp, "run_tick", {"tickers": ["UP.US"], "confirm": True})
    job = out["job"]
    assert job["kind"] == "tick"
    assert job["params"]["dry_run"] is True
    done = await call(mcp, "wait_for_job", {"job_id": job["id"], "poll_seconds": 0.05})
    assert done["job"]["status"] == "succeeded", done["job"]["error"]


@pytest.mark.anyio
async def test_real_tick_preview_reads_the_api_broker_route(mcp):
    # GET /api/brokers reports the default simulated broker.
    preview = await call(mcp, "run_tick", {"tickers": ["UP.US"], "dry_run": False})
    assert preview["live_trading"] == "off"


@pytest.mark.anyio
async def test_real_tick_refused_when_broker_mode_unknown(test_client):
    inner = bridge(test_client)

    async def no_broker_route(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/api/brokers":
            return httpx2.Response(404, json={"title": "Not Found", "status": 404})
        return await inner.handle_async_request(request)

    api = ApiClient(BASE, token=API_TOKEN, transport=httpx2.MockTransport(no_broker_route))
    async with Client(build_server(api)) as mcp:
        args = {"tickers": ["UP.US"], "dry_run": False}
        preview = await call(mcp, "run_tick", args)
        assert preview["live_trading"] == "unknown"
        assert any("refusing" in w for w in preview["warnings"])
        err = await call_error(mcp, "run_tick", {**args, "confirm": True})
        assert "refusing" in err
        assert (await call(mcp, "list_jobs"))["total"] == 0


def _broker_route_transport(tc: TestClient, broker_info: dict) -> httpx2.MockTransport:
    inner = bridge(tc)

    async def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/api/brokers":
            return httpx2.Response(200, json=broker_info)
        return await inner.handle_async_request(request)

    return httpx2.MockTransport(handler)


@pytest.mark.anyio
async def test_real_tick_allowed_only_with_paper_broker(test_client):
    def broker(kind: str, paper: bool) -> dict:
        return {"kind": kind, "paper": paper, "allow_live": True, "credentials_configured": True}

    cases = (
        (broker("simulated", paper=True), True),
        (broker("alpaca", paper=True), True),
        (broker("alpaca", paper=False), False),
    )
    for info, allowed in cases:
        api = ApiClient(BASE, token=API_TOKEN, transport=_broker_route_transport(test_client, info))
        async with Client(build_server(api)) as c:
            args = {"tickers": ["UP.US"], "dry_run": False, "confirm": True}
            result = await c.call_tool("run_tick", args)
            assert result.is_error is (not allowed), result.content[0].text
            if not allowed:
                assert "real money" in result.content[0].text


@pytest.mark.anyio
async def test_guarded_write_without_token_explains(test_client, seeded):
    async with Client(build_server(_api(test_client, token=None))) as c:
        # previews still work (reads are open on loopback)...
        assert not (
            await c.call_tool("promote_strategy", {"strategy_id": seeded["shadow_id"]})
        ).is_error
        # ...but applying needs the token
        result = await c.call_tool(
            "promote_strategy", {"strategy_id": seeded["shadow_id"], "confirm": True}
        )
        assert result.is_error
        assert "STONKS_API_TOKEN" in result.content[0].text


# ---- errors and secrets -------------------------------------------------------------


@pytest.mark.anyio
async def test_unreachable_api_gives_friendly_error():
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    api = ApiClient(BASE, token=API_TOKEN, transport=httpx2.MockTransport(refuse))
    async with Client(build_server(api)) as c:
        for name, args in (("get_portfolio", {}), ("promote_strategy", {"strategy_id": "x"})):
            result = await c.call_tool(name, args)
            assert result.is_error
            text = result.content[0].text
            assert "stonks serve" in text and BASE in text


@pytest.mark.anyio
async def test_wait_for_job_times_out_and_clamps():
    polls = []

    def running(request: httpx2.Request) -> httpx2.Response:
        polls.append(request)
        return httpx2.Response(200, json={"id": "j1", "status": "running", "progress": 0.1})

    api = ApiClient(BASE, token=API_TOKEN, transport=httpx2.MockTransport(running))
    async with Client(build_server(api, max_wait_seconds=0.2)) as c:
        result = await c.call_tool(
            "wait_for_job", {"job_id": "j1", "timeout_seconds": 3600, "poll_seconds": 0.05}
        )
    assert result.structured_content["timed_out"] is True
    assert result.structured_content["job"]["status"] == "running"
    assert 2 <= len(polls) < 20


@pytest.mark.anyio
async def test_portfolio_summary_resource(mcp):
    resources = (await mcp.list_resources()).resources
    assert [str(r.uri) for r in resources] == ["stonks://portfolio/summary"]
    read = await mcp.read_resource("stonks://portfolio/summary")
    summary = json.loads(read.contents[0].text)
    assert summary["positions"][0]["ticker"] == "UP.US"
    assert summary["total_value"] > 0


@pytest.mark.anyio
async def test_token_never_in_tool_output_or_logs(test_client, seeded, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    wrong = "wrong-token-" + "z" * 12

    outputs: list[str] = []
    for token in (API_TOKEN, wrong):
        async with Client(build_server(_api(test_client, token=token))) as c:
            for name, args in (
                ("get_portfolio", {}),
                ("promote_strategy", {"strategy_id": seeded["shadow_id"], "confirm": True}),
                ("run_tick", {"confirm": True, "tickers": ["UP.US"]}),
                ("run_ingest", {"kind": "prices", "tickers": ["NEW.US"]}),
            ):
                result = await c.call_tool(name, args)
                outputs.append(json.dumps(result.model_dump(mode="json")))
    captured = capsys.readouterr()
    haystack = "\n".join([*outputs, caplog.text, captured.out, captured.err])
    assert "401" in haystack  # the wrong token was rejected...
    assert API_TOKEN not in haystack  # ...and neither token leaked
    assert wrong not in haystack


@pytest.mark.anyio
@pytest.mark.parametrize("sid", ["../ticks#", "..%2Fticks", "x/../../ticks"])
async def test_ids_cannot_redirect_a_confirmed_write(mcp, sid):
    """A crafted id must not turn promote into e.g. POST /api/ticks."""
    err = await call_error(mcp, "promote_strategy", {"strategy_id": sid, "confirm": True})
    assert "invalid id" in err
    assert (await call(mcp, "list_jobs"))["total"] == 0
    assert (await call(mcp, "list_ticks"))["total"] == 1
