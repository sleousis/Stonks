"""Guarded MCP status tools pass reason / override to the governed API
(W1.6), end to end over the real FastAPI app."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from stonks.api import create_app
from stonks.mcp.server import build_server
from tests.integration.app.test_mcp_server import _api, call, call_error
from tests.integration.app.test_studio import TREND

LONG_REASON = "owner override: incubation cut short, see ticket 42"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def test_client(settings, seeded, fake_source):
    app = create_app(settings, source_factory=lambda: fake_source, sse_poll_seconds=0.02)
    with TestClient(app, client=("127.0.0.1", 50000)) as tc:
        yield tc


@pytest.fixture
async def mcp(test_client):
    async with Client(build_server(_api(test_client), max_wait_seconds=120)) as c:
        yield c


@pytest.mark.anyio
async def test_promote_with_override_and_reason(mcp, seeded):
    sid = seeded["shadow_id"]
    preview = await call(
        mcp, "promote_strategy", {"strategy_id": sid, "override": True, "reason": LONG_REASON}
    )
    assert preview["preview"] is True
    assert preview["reason"] == LONG_REASON and preview["override"] is True
    out = await call(
        mcp,
        "promote_strategy",
        {"strategy_id": sid, "override": True, "reason": LONG_REASON, "confirm": True},
    )
    assert out["applied"] is True
    assert out["strategy"]["status"] == "active"
    change = out["strategy"]["status_history"][-1]
    assert change["override"] is True and change["actor"] == "user:usr_owner"


@pytest.mark.anyio
async def test_refused_promotion_explains_the_gate(mcp, seeded):
    err = await call_error(
        mcp, "promote_strategy", {"strategy_id": seeded["shadow_id"], "confirm": True}
    )
    assert "go-live" in err
    assert "override" in err  # the hint says how to proceed


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tool", "status"), [("retire_strategy", "retired"), ("shadow_strategy", "shadow")]
)
async def test_demotions_with_reason(mcp, seeded, tool, status):
    out = await call(
        mcp, tool, {"strategy_id": seeded["active_id"], "reason": "edge decayed", "confirm": True}
    )
    assert out["strategy"]["status"] == status
    assert out["strategy"]["status_history"][-1]["reason"] == "edge decayed"


@pytest.mark.anyio
async def test_strategy_history_tool(mcp, seeded):
    history = await call(mcp, "get_strategy_history", {"strategy_id": seeded["active_id"]})
    assert [c["to_status"] for c in history["items"]] == ["active"]


@pytest.mark.anyio
async def test_enable_and_disable_draft_take_reason(mcp):
    draft = await call(mcp, "create_draft", {"name": "trend", "spec": TREND})
    await call(mcp, "register_draft", {"draft_id": draft["id"], "confirm": True})
    on = await call(
        mcp,
        "enable_draft",
        {"draft_id": draft["id"], "override": True, "reason": LONG_REASON, "confirm": True},
    )
    assert on["draft"]["strategy_status"] == "active"
    off = await call(
        mcp, "disable_draft", {"draft_id": draft["id"], "reason": "back to paper", "confirm": True}
    )
    assert off["draft"]["strategy_status"] == "shadow"
