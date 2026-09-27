"""MCP options tools end to end (MCP client -> MCP server -> ApiClient ->
the FastAPI app) over synthetic chains: reads, a payoff, and an options
backtest job waited on with its typed result."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from stonks.api import create_app
from stonks.mcp.server import build_server
from tests.integration.app.test_api_options import seed_option_lake
from tests.integration.app.test_mcp_server import _api, call, call_error


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def test_client(settings, seeded, fake_source):
    seed_option_lake(settings.lake.path)
    app = create_app(settings, source_factory=lambda: fake_source, sse_poll_seconds=0.02)
    with TestClient(app, client=("127.0.0.1", 50000)) as tc:
        yield tc


@pytest.fixture
async def mcp(test_client):
    async with Client(build_server(_api(test_client), max_wait_seconds=120)) as c:
        yield c


@pytest.mark.anyio
async def test_reads_and_payoff(mcp):
    listed = await call(mcp, "list_option_underlyings")
    assert [u["underlying"] for u in listed["items"]] == ["UP.US"]
    chain = await call(mcp, "get_option_chain", {"underlying": "UP.US", "as_of": "2026-02-02"})
    assert chain["as_of"] == "2026-02-02" and chain["rows"]
    strategies = await call(mcp, "list_option_strategies")
    assert "covered_call" in {s["id"] for s in strategies["items"]}
    structures = await call(mcp, "list_option_structures")
    assert "iron_condor" in {s["name"] for s in structures["items"]}
    payoff = await call(
        mcp,
        "get_option_payoff",
        {"underlying": "UP.US", "structure": "iron_condor", "as_of": "2026-02-02"},
    )
    assert len(payoff["legs"]) == 4 and len(payoff["breakevens"]) == 2
    error = await call_error(mcp, "get_option_chain", {"underlying": "NOPE.US"})
    assert "no stored option chain" in error


@pytest.mark.anyio
async def test_backtest_job_and_typed_result(mcp):
    job = await call(
        mcp,
        "run_options_backtest",
        {
            "strategy": "vertical_spread",
            "underlyings": ["UP.US"],
            "start": "2026-01-02",
            "end": "2026-03-31",
            "validation": False,
        },
    )
    assert job["kind"] == "options_backtest"
    waited = await call(mcp, "wait_for_job", {"job_id": job["id"]})
    assert waited["job"]["status"] == "succeeded", waited
    result = waited["result"]
    assert result["strategy"] == "vertical_spread" and result["verdict"] == "not_run"
    assert result["equity"] and result["synthetic"] is True
