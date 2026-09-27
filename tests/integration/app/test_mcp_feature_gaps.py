"""MCP tools added for feature completeness (roadmap 18.7), end to end:
the go-live report, the schedule, the notification feed, run_lab on a
stored universe with ensure_data, sweeps, cancelling a job, deleting a
draft, journal notes, the survival catalogue, and the old tool names."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from stonks.accounts import DEFAULT_OWNER_ID
from stonks.api import create_app
from stonks.mcp.server import build_server
from stonks.mcp.tools.common import ALIASES
from stonks.store.lake import DuckDBLake
from stonks.universes import UniverseDefinition, UniverseStore, refresh_universe
from tests.fixtures.universes import FakeListingSource, bars
from tests.integration.app.test_api import AUTH
from tests.integration.app.test_mcp_server import _api, call, call_error
from tests.integration.app.test_studio import TREND

WINDOW = {"start": "2025-10-01", "end": "2026-04-01"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def source():
    return FakeListingSource(prices={"NEW.US": bars("NEW.US", date(2025, 6, 2), date(2026, 4, 1))})


@pytest.fixture
def app(settings, seeded, source):
    settings.api.allowed_hosts = ["testserver", "127.0.0.1"]
    return create_app(settings, source_factory=lambda: source, sse_poll_seconds=0.02)


@pytest.fixture
def test_client(app):
    with TestClient(app, client=("127.0.0.1", 50000)) as tc:
        yield tc


@pytest.fixture
async def mcp(test_client):
    async with Client(build_server(_api(test_client), max_wait_seconds=120)) as c:
        yield c


async def _wait(mcp, job_id):
    done = await call(mcp, "wait_for_job", {"job_id": job_id, "poll_seconds": 0.05})
    assert done["job"]["status"] == "succeeded", done["job"]["error"]
    return done


@pytest.mark.anyio
async def test_the_go_live_report_shows_the_gate_before_a_promotion(mcp):
    report = await call(mcp, "get_golive_report", {"strategy_id": "bah_active"})
    assert report["strategy_id"] == "bah_active"
    assert report["checks"] and {"name", "passed"} <= set(report["checks"][0])
    err = await call_error(mcp, "get_golive_report", {"strategy_id": "nope"})
    assert "not found" in err.lower()


@pytest.mark.anyio
async def test_the_schedule_says_what_runs_next_and_the_market_session(mcp):
    schedule = await call(mcp, "get_schedule", {"limit": 5})
    assert {"jobs", "recent", "market"} <= set(schedule)


@pytest.mark.anyio
async def test_the_feed_can_be_read_and_marked_read(mcp, test_client):
    sent = test_client.post("/api/notifications/test", headers=AUTH)
    assert sent.status_code == 201, sent.text
    feed = await call(mcp, "list_notifications", {"unread_only": True})
    assert feed["unread_count"] >= 1 and feed["items"][0]["title"] == "Test notification"
    marked = await call(mcp, "mark_notifications_read", {"ids": [feed["items"][0]["id"]]})
    assert marked["updated"] == 1
    assert (await call(mcp, "mark_notifications_read"))["unread_count"] == 0
    alerts = await call(mcp, "list_alerts", {"limit": 5})
    assert "items" in alerts


@pytest.mark.anyio
async def test_event_alert_switches_read_and_change_with_a_confirm(mcp):
    prefs = await call(mcp, "get_notification_preferences")
    assert [e["topic"] for e in prefs["event_alerts"]] == ["earnings", "dividends", "economic"]
    preview = await call(mcp, "set_event_alerts", {"earnings": False})
    assert preview["applied"] is False
    assert {e["topic"]: e["enabled"] for e in preview["event_alerts"]}["earnings"] is True
    done = await call(mcp, "set_event_alerts", {"earnings": False, "confirm": True})
    assert done["applied"] is True
    assert {e["topic"]: e["enabled"] for e in done["event_alerts"]}["earnings"] is False
    err = await call_error(mcp, "set_event_alerts", {"confirm": True})
    assert "at least one" in err.lower()
    await call(mcp, "set_event_alerts", {"earnings": True, "confirm": True})


@pytest.mark.anyio
async def test_run_lab_on_a_stored_universe_fetches_missing_data_first(mcp, settings, source):
    with DuckDBLake(settings.lake.path) as lake:
        UniverseStore(lake).save(
            UniverseDefinition(id="mine", kind="list", spec={"tickers": ["UP.US", "NEW.US"]})
        )
        refresh_universe(lake, "mine", as_of=date(2026, 3, 1))
    job = await call(
        mcp,
        "run_lab",
        {
            "class_path": "stonks.strategies.examples.buy_and_hold:BuyAndHold",
            "universe_id": "mine",
            "ensure_data": True,
            "strict_preflight": False,
            "budget": 1,
            "preset": "quick",
            **WINDOW,
        },
    )
    done = await _wait(mcp, job["id"])
    ensure_id = done["result"]["ensure_job_id"]
    assert ensure_id
    report = await _wait(mcp, ensure_id)  # the lab_ensure result route is reachable
    assert report["job"]["kind"] == "lab_ensure" and report["result"]["tickers_fetched"] == 1
    assert {c[0] for c in source.price_calls} == {"NEW.US"}


@pytest.mark.anyio
async def test_run_lab_needs_tickers_or_a_universe(mcp):
    err = await call_error(mcp, "run_lab", {"class_path": "x.y:Z", **WINDOW})
    assert "universe" in err


@pytest.mark.anyio
async def test_a_sweep_ranks_every_strategy_named(mcp):
    job = await call(
        mcp,
        "run_sweep",
        {
            "universe": ["UP.US", "DOWN.US"],
            "strategies": ["buy_and_hold", "momentum"],
            "budget": 1,
            "survival_tests": ["oos"],
            "cost_model": "zero",
            **WINDOW,
        },
    )
    assert job["kind"] == "lab_sweep"
    done = await _wait(mcp, job["id"])
    assert {r["strategy"] for r in done["result"]["rows"]} == {"buy_and_hold", "momentum"}


@pytest.mark.anyio
async def test_cancel_a_queued_job(mcp, app, test_client):
    job = app.state.services.runner.store.create("backtest", {}, owner_id=DEFAULT_OWNER_ID)
    cancelled = await call(mcp, "cancel_job", {"job_id": job.id})
    assert cancelled["status"] == "cancelled"


@pytest.mark.anyio
async def test_deleting_a_draft_needs_confirm(mcp):
    draft = await call(mcp, "create_draft", {"name": "trend", "spec": TREND})
    preview = await call(mcp, "delete_draft", {"draft_id": draft["id"]})
    assert preview["preview"] is True and preview["draft"]["name"] == "trend"
    assert (await call(mcp, "get_draft", {"draft_id": draft["id"]}))["id"] == draft["id"]
    done = await call(mcp, "delete_draft", {"draft_id": draft["id"], "confirm": True})
    assert done["applied"] is True
    assert "not found" in (await call_error(mcp, "get_draft", {"draft_id": draft["id"]})).lower()


@pytest.mark.anyio
async def test_journal_notes_can_be_added_and_edited(mcp):
    [order] = (await call(mcp, "list_orders"))["items"]
    added = await call(mcp, "add_journal_note", {"client_id": order["client_id"], "note": "ok"})
    edited = await call(mcp, "edit_journal_note", {"note_id": added["id"], "note": "better"})
    assert edited["note"] == "better"
    ticket = await call(mcp, "get_order_tca", {"client_id": order["client_id"]})
    assert [n["note"] for n in ticket["notes"]] == ["better"]


@pytest.mark.anyio
async def test_the_survival_catalogue_and_studio_capabilities_come_from_the_api(mcp):
    tests = await call(mcp, "list_survival_tests")
    assert "oos" in {t["id"] for t in tests["items"]}
    presets = await call(mcp, "list_survival_presets")
    assert {"quick", "standard", "promotion"} <= {p["name"] for p in presets["items"]}
    caps = await call(mcp, "get_studio_capabilities")
    assert caps["code_strategies"] is False


@pytest.mark.anyio
async def test_old_tool_names_still_work_and_say_they_are_deprecated(mcp):
    tools = {t.name: t for t in (await mcp.list_tools()).tools}
    for old, new in ALIASES.items():
        assert new in tools and old in tools, (old, new)
        assert tools[old].description.startswith(f"Deprecated alias of {new}.")
        assert tools[old].input_schema == tools[new].input_schema
    assert await call(mcp, "health") == await call(mcp, "get_api_health")


@pytest.mark.anyio
async def test_the_trial_ledger_can_be_read_back(mcp):
    job = await call(
        mcp,
        "run_lab",
        {
            "class_path": "stonks.strategies.examples.momentum:Momentum",
            "universe": ["UP.US", "DOWN.US"],
            "budget": 2,
            "preset": "quick",
            "hypothesis": "trend persists",
            **WINDOW,
        },
    )
    done = await _wait(mcp, job["id"])
    runs = await call(mcp, "list_ledger_runs")
    assert [r["id"] for r in runs["items"]] == [done["result"]["run_id"]]
    assert runs["items"][0]["hypothesis"] == "trend persists"
    detail = await call(mcp, "get_ledger_run", {"run_id": runs["items"][0]["id"]})
    assert len(detail["trials"]) == detail["n_trials"] == 2
