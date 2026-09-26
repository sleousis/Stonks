"""MCP run_lab / lab_run_draft pass register_if_passes, preset, cost_model
and the hypothesis card through to the API (Integration 1)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from stonks.api import create_app
from stonks.mcp.server import build_server
from tests.integration.app.test_mcp_server import _api, call
from tests.integration.app.test_studio import TREND

MOMENTUM = "stonks.strategies.examples.momentum:Momentum"
WINDOW = {"universe": ["UP.US", "DOWN.US"], "start": "2025-10-01", "end": "2026-04-01"}


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


async def _wait(mcp, job):
    done = await call(mcp, "wait_for_job", {"job_id": job["id"], "poll_seconds": 0.05})
    assert done["job"]["status"] == "succeeded", done["job"]["error"]
    return done


@pytest.mark.anyio
async def test_register_if_passes_is_guarded(mcp):
    args = {"class_path": MOMENTUM, **WINDOW, "budget": 1, "register_if_passes": True}
    preview = await call(mcp, "run_lab", args)
    assert preview["preview"] is True
    assert any("only if every survival test passes" in w for w in preview["warnings"])
    assert (await call(mcp, "list_jobs"))["total"] == 0


@pytest.mark.anyio
async def test_run_lab_passes_preset_cost_model_and_hypothesis(mcp, test_client):
    job = await call(
        mcp,
        "run_lab",
        {
            "class_path": MOMENTUM,
            **WINDOW,
            "budget": 1,
            "preset": "quick",
            "cost_model": "zero",
            "hypothesis": "trend persists",
        },
    )
    done = await _wait(mcp, job)
    result = done["result"]
    assert [r["test_id"] for r in result["survival_reports"]] == ["oos", "period_stability"]
    assert result["run_id"] and result["n_trials_run"] == 1


@pytest.mark.anyio
async def test_lab_run_draft_takes_the_new_options(mcp):
    draft = await call(mcp, "create_draft", {"name": "trend", "spec": TREND})
    job = await call(
        mcp,
        "lab_run_draft",
        {
            "draft_id": draft["id"],
            **WINDOW,
            "budget": 1,
            "survival_tests": ["oos"],
            "register_if_passes": True,
            "cost_model": "realistic",
            "confirm": True,
        },
    )
    done = await _wait(mcp, job["job"])
    result = done["job"]["result"]
    registered = result["registered_strategy_id"]
    assert (registered is not None) == (result["verdict"] == "pass")
