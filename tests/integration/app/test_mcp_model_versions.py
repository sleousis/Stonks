"""Roadmap 22.6 over MCP: the version tools preview without confirm and go
through the governed API with it, end to end over the real FastAPI app."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from stonks.api import create_app
from stonks.mcp.server import build_server
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState
from tests.fixtures.lifecycle import MeanFit
from tests.integration.app.test_mcp_server import _api, call, call_error

LONG_REASON = "owner swap: the refit handles the new regime, see ticket 7"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def test_client(settings, seeded, fake_source):
    with SqliteState(settings.state.path) as state:
        StrategyRegistry(state=state, artifacts_dir=settings.registry.artifacts_dir).register(
            MeanFit({"ticker": "UP.US"}), reports=[], strategy_id="mf"
        )
    app = create_app(settings, source_factory=lambda: fake_source, sse_poll_seconds=0.02)
    with TestClient(app, client=("127.0.0.1", 50000)) as tc:
        yield tc


@pytest.fixture
async def mcp(test_client):
    async with Client(build_server(_api(test_client), max_wait_seconds=120)) as c:
        yield c


async def _retrain(mcp) -> None:
    args = {"strategy_ids": ["mf"], "as_of": "2026-03-18", "tickers": ["UP.US"]}
    preview = await call(mcp, "retrain_models", args)
    assert preview["preview"] is True and preview["applied"] is False
    queued = await call(mcp, "retrain_models", {**args, "confirm": True})
    done = await call(mcp, "wait_for_job", {"job_id": queued["job"]["id"]})
    assert done["job"]["status"] == "succeeded", done
    assert done["result"]["candidates"] == 1


@pytest.mark.anyio
async def test_retrain_preview_check_and_swap(mcp):
    await _retrain(mcp)
    versions = await call(mcp, "list_model_versions", {"strategy_id": "mf"})
    assert [(v["version"], v["status"]) for v in versions["items"]] == [
        (1, "live"),
        (2, "candidate"),
    ]
    candidates = await call(mcp, "list_model_candidates", {})
    assert [c["book_id"] for c in candidates["items"]] == ["mf@v2"]
    check = await call(mcp, "check_model_swap", {"strategy_id": "mf", "version": 2})
    assert check["passed"] is False

    preview = await call(mcp, "swap_model_version", {"strategy_id": "mf", "version": 2})
    assert preview["preview"] is True and preview["warnings"] == ["the swap check fails"]
    err = await call_error(
        mcp, "swap_model_version", {"strategy_id": "mf", "version": 2, "confirm": True}
    )
    assert "swap check" in err and "override" in err

    out = await call(
        mcp,
        "swap_model_version",
        {"strategy_id": "mf", "version": 2, "confirm": True, "override": True,
         "reason": LONG_REASON},
    )  # fmt: skip
    assert out["applied"] is True and out["version"]["status"] == "live"
    history = await call(mcp, "get_model_version_history", {"strategy_id": "mf"})
    assert history["items"][-1]["actor"] == "user:usr_owner"


@pytest.mark.anyio
async def test_reject_preview_then_confirm(mcp):
    await _retrain(mcp)
    preview = await call(
        mcp, "reject_model_version", {"strategy_id": "mf", "version": 2, "reason": "worse"}
    )
    assert preview["preview"] is True and preview["version"]["status"] == "candidate"
    out = await call(
        mcp,
        "reject_model_version",
        {"strategy_id": "mf", "version": 2, "reason": "worse", "confirm": True},
    )
    assert out["version"]["status"] == "rejected"
