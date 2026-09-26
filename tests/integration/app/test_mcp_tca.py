"""MCP TCA tools end to end (MCP client -> MCP server -> ApiClient -> the
FastAPI app): the summary, the journal and one order, all read-only."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from stonks.api import create_app
from stonks.mcp.server import build_server
from stonks.store.state import SqliteState
from tests.integration.app.test_mcp_server import _api, call

CLIENT_ID = "2026-03-20:mom:TCA.US:buy"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def test_client(settings, seeded, fake_source):
    settings.api.allowed_hosts = ["testserver"]
    with SqliteState(settings.state.path) as state:
        state.execute(
            "INSERT INTO orders (client_id, ticker, side, quantity, order_type, status,"
            " created_at, updated_at, portfolio_id, decision_price, decided_at,"
            " decision_context_json)"
            " VALUES (?, 'TCA.US', 'buy', 10, 'market', 'filled', ?, ?, 'pf_default', 100.0, ?,"
            ' \'{"trigger": "signal"}\')',
            [CLIENT_ID, *["2026-03-20T00:00:00+00:00"] * 3],
        )
        state.execute(
            "INSERT INTO fills (order_client_id, ticker, quantity, price, fee, filled_at,"
            " arrival_price) VALUES (?, 'TCA.US', 10, 101.0, 1.0, ?, 100.0)",
            [CLIENT_ID, "2026-03-20T00:00:00+00:00"],
        )
    app = create_app(settings, source_factory=lambda: fake_source)
    with TestClient(app, client=("127.0.0.1", 50000)) as tc:
        yield tc


@pytest.fixture
async def mcp(test_client):
    async with Client(build_server(_api(test_client), max_wait_seconds=60)) as c:
        yield c


@pytest.mark.anyio
async def test_tca_tools_read_the_ledger(mcp):
    summary = await call(mcp, "tca_summary", {"by": "ticker", "ticker": "TCA.US"})
    [group] = summary["groups"]
    assert group["key"] == "TCA.US"
    assert group["is_bps"] == pytest.approx(110.0)  # 100 bps impact + 10 bps fee
    journal = await call(mcp, "trade_journal", {"ticker": "TCA.US"})
    assert [e["client_id"] for e in journal["items"]] == [CLIENT_ID]
    order = await call(mcp, "order_tca", {"client_id": CLIENT_ID})
    assert order["trigger"] == "signal"
    assert order["shortfall"]["impact_bps"] == pytest.approx(100.0)
