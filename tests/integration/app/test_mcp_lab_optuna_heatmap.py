"""MCP run_lab passes the Optuna tuner, its sampler and pruning, the new
objectives and the heatmap option through to the API (22.1, 22.5)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from stonks.api import create_app
from stonks.mcp.server import build_server
from tests.integration.app.test_mcp_server import _api, call, call_error

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


@pytest.mark.anyio
async def test_run_lab_with_optuna_and_a_heatmap(mcp):
    job = await call(
        mcp,
        "run_lab",
        {
            "class_path": MOMENTUM,
            **WINDOW,
            "tuner": "optuna",
            "sampler": "tpe",
            "prune": True,
            "objective": "calmar",
            "budget": 3,
            "survival_tests": ["plateau"],
            "heatmap": {"x": "lookback_days", "y": "threshold", "grid_size": 2},
        },
    )
    assert job["params"]["tuner"] == "optuna"
    sent = job["params"]["heatmap"]
    assert (sent["x"], sent["y"], sent["grid_size"]) == ("lookback_days", "threshold", 2)
    assert sent["fast"] is True  # the API default
    done = await call(mcp, "wait_for_job", {"job_id": job["id"], "poll_seconds": 0.05})
    assert done["job"]["status"] == "succeeded", done["job"]["error"]
    heatmap = done["result"]["heatmap"]
    assert (heatmap["x"], heatmap["y"]) == ("lookback_days", "threshold")
    assert heatmap["plateau"] is not None
    cells = len(heatmap["x_values"]) * len(heatmap["y_values"])
    assert done["result"]["n_trials_run"] == 3 + cells


@pytest.mark.anyio
async def test_a_bad_heatmap_axis_is_refused(mcp):
    err = await call_error(
        mcp, "run_lab", {"class_path": MOMENTUM, **WINDOW, "heatmap": {"x": "nope"}}
    )
    assert "422" in err and "heatmap" in err
