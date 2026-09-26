"""Strategy Studio MCP tools end to end over the real FastAPI app (same
httpx2 MockTransport bridge as test_mcp_server)."""

from __future__ import annotations

import sys

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from stonks.api import create_app
from stonks.app.studio import USER_MODULE_PREFIX
from stonks.mcp.server import build_server
from tests.integration.app.test_mcp_server import _api, call, call_error
from tests.integration.app.test_studio import GOOD_CODE, TREND

WINDOW = {"universe": ["UP.US", "DOWN.US"], "start": "2025-10-01", "end": "2026-04-01"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _forget_user_modules():
    yield
    for name in [m for m in sys.modules if m.startswith(USER_MODULE_PREFIX)]:
        del sys.modules[name]


@pytest.fixture
def test_client(settings, seeded, fake_source):
    app = create_app(settings, source_factory=lambda: fake_source, sse_poll_seconds=0.02)
    with TestClient(app, client=("127.0.0.1", 50000)) as tc:
        yield tc


@pytest.fixture
async def mcp(test_client):
    async with Client(build_server(_api(test_client), max_wait_seconds=120)) as c:
        yield c


async def _draft(mcp, **extra) -> dict:
    return await call(mcp, "create_draft", {"name": "trend", "spec": TREND, **extra})


async def _wait(mcp, job: dict) -> dict:
    done = await call(mcp, "wait_for_job", {"job_id": job["id"], "poll_seconds": 0.05})
    assert done["job"]["status"] == "succeeded", done["job"]["error"]
    return done


@pytest.mark.anyio
async def test_templates_schema_and_spec_validation(mcp):
    templates = (await call(mcp, "list_studio_templates"))["items"]
    assert {t["id"] for t in templates} >= {"sma_trend_following", "rsi_mean_reversion"}
    assert (await call(mcp, "get_rule_schema"))["title"] == "RuleSpec"
    ok = await call(mcp, "validate_rule_spec", {"spec": TREND})
    assert ok == {"valid": True, "issues": [], "smoke": None}
    bad = await call(mcp, "validate_rule_spec", {"spec": {**TREND, "rank": {"by": "x"}}})
    assert bad["valid"] is False and bad["issues"]


@pytest.mark.anyio
async def test_draft_crud_and_validate(mcp):
    draft = await _draft(mcp)
    assert draft["status"] == "draft" and draft["kind"] == "rule"
    listed = await call(mcp, "list_drafts")
    assert [d["id"] for d in listed["items"]] == [draft["id"]]
    assert (await call(mcp, "get_draft", {"draft_id": draft["id"]}))["name"] == "trend"
    updated = await call(mcp, "update_draft", {"draft_id": draft["id"], "name": "trend 2"})
    assert updated["name"] == "trend 2" and updated["spec"] == draft["spec"]
    checked = await call(mcp, "validate_draft", {"draft_id": draft["id"]})
    assert checked["valid"] is True and checked["smoke"]["data"] == "sample"
    assert "update_draft needs" in await call_error(mcp, "update_draft", {"draft_id": draft["id"]})


@pytest.mark.anyio
async def test_lab_run_draft_registering_needs_confirm(mcp):
    """register_strategy=true registers like register_draft, so it is guarded too."""
    draft = await _draft(mcp)
    args = {"draft_id": draft["id"], **WINDOW, "budget": 1, "survival_tests": ["oos"]}
    preview = await call(mcp, "lab_run_draft", {**args, "register_strategy": True})
    assert preview["preview"] is True and preview["applied"] is False
    assert "confirm=true" in preview["next_step"]
    assert (await call(mcp, "list_jobs"))["total"] == 0

    out = await call(mcp, "lab_run_draft", {**args, "register_strategy": True, "confirm": True})
    done = await _wait(mcp, out["job"])
    assert done["result"]["registered_strategy_id"]


@pytest.mark.anyio
async def test_backtest_and_lab_run_a_draft(mcp):
    draft = await _draft(mcp)
    job = await call(mcp, "backtest_draft", {"draft_id": draft["id"], **WINDOW})
    done = await _wait(mcp, job)
    assert "final_return" in done["result"] and done["result"]["equity"]

    job = await call(
        mcp,
        "lab_run_draft",
        {"draft_id": draft["id"], **WINDOW, "budget": 1, "survival_tests": ["oos"]},
    )
    assert job["kind"] == "studio_lab_run"
    done = await _wait(mcp, job)
    assert done["result"]["verdict"] in ("pass", "fail")


@pytest.mark.anyio
async def test_register_enable_disable_are_guarded(mcp):
    draft = await _draft(mcp)
    did = draft["id"]

    preview = await call(mcp, "register_draft", {"draft_id": did})
    assert preview["preview"] is True and preview["new_status"] == "shadow"
    assert (await call(mcp, "get_draft", {"draft_id": did}))["status"] == "draft"
    assert (await call(mcp, "list_strategies", {"status": "shadow"}))["total"] == 1

    early = await call(mcp, "enable_draft", {"draft_id": did})
    assert any("not registered" in w for w in early["warnings"])

    applied = await call(mcp, "register_draft", {"draft_id": did, "confirm": True})
    assert applied["applied"] is True
    sid = applied["draft"]["registered_strategy_id"]
    assert applied["draft"]["strategy_status"] == "shadow"

    preview = await call(mcp, "enable_draft", {"draft_id": did})
    assert preview["strategy"]["strategy_id"] == sid
    assert preview["strategy"]["current_status"] == "shadow"
    assert (await call(mcp, "get_strategy", {"strategy_id": sid}))["status"] == "shadow"

    enabled = await call(mcp, "enable_draft", {"draft_id": did, "confirm": True})
    assert enabled["draft"]["strategy_status"] == "active"
    assert (await call(mcp, "get_strategy", {"strategy_id": sid}))["status"] == "active"

    assert (await call(mcp, "disable_draft", {"draft_id": did}))["new_status"] == "shadow"
    assert (await call(mcp, "get_strategy", {"strategy_id": sid}))["status"] == "active"
    await call(mcp, "disable_draft", {"draft_id": did, "confirm": True})
    assert (await call(mcp, "get_strategy", {"strategy_id": sid}))["status"] == "shadow"


@pytest.mark.anyio
async def test_code_drafts_disabled_surface_the_403(mcp, settings):
    err = await call_error(
        mcp, "create_draft", {"name": "c", "kind": "code", "source_code": GOOD_CODE}
    )
    assert "403" in err
    assert "allow_code_strategies" in err
    assert "code strategies are disabled" in err.lower()

    settings.api.allow_code_strategies = True
    draft = await call(mcp, "create_draft", {"name": "c", "kind": "code", "source_code": GOOD_CODE})
    settings.api.allow_code_strategies = False
    for tool, args in (
        ("validate_draft", {}),
        ("backtest_draft", WINDOW),
        ("register_draft", {"confirm": True}),
    ):
        err = await call_error(mcp, tool, {"draft_id": draft["id"], **args})
        assert "403" in err and "allow_code_strategies" in err, tool


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("get_draft", {}),
        ("update_draft", {"name": "x"}),
        ("validate_draft", {}),
        ("backtest_draft", WINDOW),
        ("lab_run_draft", WINDOW),
        ("register_draft", {"confirm": True}),
        ("enable_draft", {"confirm": True}),
        ("disable_draft", {"confirm": True}),
    ],
)
async def test_draft_ids_are_validated(mcp, tool, args):
    err = await call_error(mcp, tool, {"draft_id": "../../ticks#", **args})
    assert "invalid id" in err
    assert (await call(mcp, "list_jobs"))["total"] == 0


@pytest.mark.anyio
async def test_guarded_draft_write_without_token_explains(test_client):
    async with Client(build_server(_api(test_client))) as c:
        draft = await _draft(c)
    async with Client(build_server(_api(test_client, token=None))) as c:
        preview = await call(c, "register_draft", {"draft_id": draft["id"]})
        assert preview["preview"] is True
        err = await call_error(c, "register_draft", {"draft_id": draft["id"], "confirm": True})
        assert "STONKS_API_TOKEN" in err
