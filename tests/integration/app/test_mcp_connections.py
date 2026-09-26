"""MCP connection and signal-research tools end to end (MCP client -> MCP
server -> ApiClient -> the FastAPI app with the fake broker providers)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from stonks.api import create_app
from stonks.app.connections import ConnectionsAppService
from stonks.app.context import AppContext
from stonks.app.services import Services
from stonks.connections.providers import fake
from stonks.connections.ratelimit import reset_limiters
from stonks.connections.settings import ConnectionsConfig
from stonks.mcp.server import build_server
from stonks.security import KeyRing, SecretBox, generate_key
from tests.integration.app.test_api import AUTH
from tests.integration.app.test_mcp_server import _api, call, call_error

TOKEN = "mcp-demo-token-778899"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _clean_fakes():
    fake.FAKE_BOOKS.clear()
    reset_limiters()
    yield
    fake.FAKE_BOOKS.clear()
    reset_limiters()


@pytest.fixture
def test_client(settings, seeded, fake_source):
    settings.api.allowed_hosts = ["testserver"]
    ctx = AppContext(settings, source_factory=lambda: fake_source)
    svc = Services.create(ctx)
    svc.connections = ConnectionsAppService(
        ctx,
        config=ConnectionsConfig(enabled_providers=("fake",)),
        box=SecretBox(KeyRing.parse(f"k1:{generate_key()}")),
    )
    with TestClient(create_app(settings, services=svc), client=("127.0.0.1", 50000)) as tc:
        yield tc


@pytest.fixture
async def mcp(test_client):
    async with Client(build_server(_api(test_client), max_wait_seconds=60)) as c:
        yield c


def _connect(tc: TestClient) -> str:
    resp = tc.post(
        "/api/connections/keys",
        json={"provider": "fake", "fields": {"token": TOKEN}},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.mark.anyio
async def test_list_connections_and_accounts(mcp, test_client):
    assert (await call(mcp, "list_connections")) == {"items": []}
    cid = _connect(test_client)
    listed = await call(mcp, "list_connections")
    assert [c["id"] for c in listed["items"]] == [cid]
    assert TOKEN not in str(listed)
    accounts = await call(mcp, "get_connection_accounts", {"connection_id": cid})
    [acc] = accounts["items"]
    assert acc["external_account_id"] == "fake-acc-1"
    missing = await call_error(mcp, "get_connection_accounts", {"connection_id": "con_nope"})
    assert "404" in missing or "not found" in missing


@pytest.mark.anyio
async def test_sync_connection_is_guarded(mcp, test_client):
    cid = _connect(test_client)
    preview = await call(mcp, "sync_connection", {"connection_id": cid})
    assert preview["preview"] is True and preview["applied"] is False
    assert test_client.get(f"/api/connections/{cid}", headers=AUTH).json()["last_sync_at"] is None
    done = await call(mcp, "sync_connection", {"connection_id": cid, "confirm": True})
    assert done["applied"] is True
    assert done["result"]["status"] == "ok"
    assert test_client.get(f"/api/connections/{cid}", headers=AUTH).json()["last_sync_at"]


@pytest.mark.anyio
async def test_run_signal_ic_and_wait(mcp):
    job = await call(
        mcp,
        "run_signal_ic",
        {
            "class_path": "stonks.strategies.examples.momentum:Momentum",
            "params": {"lookback_days": 10},
            "universe": ["UP.US", "FLAT.US", "DOWN.US"],
            "start": "2025-10-01",
            "end": "2026-04-01",
        },
    )
    assert job["kind"] == "signal_ic"
    done = await call(mcp, "wait_for_job", {"job_id": job["id"], "timeout_seconds": 60})
    assert done["job"]["status"] == "succeeded"
    assert done["result"]["status"] == "n/a"  # three tickers: below the IC minimum
