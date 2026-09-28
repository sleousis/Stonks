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
    "list_universes",
    "get_universe",
    "get_universe_members",
    "get_universe_history",
    "list_universe_exchanges",
    "get_api_health",
    "health",
    "get_portfolio",
    "get_portfolio_totals",
    "list_portfolio_snapshots",
    "list_strategies",
    "get_strategy",
    "get_strategy_history",
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
    "get_risk_policy",
    "get_pnl",
    "list_shadow_decisions",
    "list_shadow_pnl",
    "get_shadow_pnl",
    "get_health_report",
    "get_stream_status",
    "get_broker",
    "list_sources",
    "list_cost_models",
    "list_studio_templates",
    "get_rule_schema",
    "validate_rule_spec",
    "list_drafts",
    "get_draft",
    "list_connections",
    "get_connection_accounts",
    "list_halts",
    "list_reconcile_reports",
    "get_reconcile_report",
    "list_statement_flags",
    "tca_summary",
    "trade_journal",
    "order_tca",
    "list_portfolios",
    "list_trading_modes",
    "list_subscriptions",
    "whoami",
    "get_insights",
    "get_strategy_agreement",
    "get_insights_totals",
    "get_live_risk",
    "live_risk",
    "list_risk_snapshots",
    "risk_snapshots",
    "list_intraday_snapshots",
    "get_tca_summary",
    "list_trade_journal",
    "get_order_tca",
    "list_round_trips",
    "get_round_trip",
    "get_pnl_calendar",
    "get_journal_breakdown",
    "list_playbooks",
    "get_golive_report",
    "list_ledger_runs",
    "get_ledger_run",
    "get_schedule",
    "list_alerts",
    "list_notifications",
    "get_notification_preferences",
    "list_survival_tests",
    "list_survival_presets",
    "get_studio_capabilities",
    "get_chart",
    "compare_tickers",
    "get_leaderboard",
    "get_tear_sheet",
    "list_watchlists",
    "get_watchlist",
    "get_my_risk_limits",
    "list_price_alerts",
    "list_price_alert_events",
    "get_tax_settings",
    "list_tax_lot_picks",
    "get_fx_rate",
    "list_order_drafts",
    "list_cash_flows",
    "list_research_sessions",
    "get_research_session",
    "list_factors",
    "get_factor",
    "check_factor_expression",
    "get_factor_values",
    "list_option_underlyings",
    "get_option_chain",
    "list_option_strategies",
    "list_option_structures",
    "get_option_payoff",
    "get_calendar",
    "get_news",
    "get_earnings_warnings",
    "list_event_alert_kinds",
    "list_screen_metrics",
    "run_screen",
    "list_screens",
    "get_screen",
    "list_model_versions",
    "get_model_version_history",
    "list_model_candidates",
    "check_model_swap",
    "list_tickets",
    "get_ticket",
    "get_live_stage",
    "get_live_gate_report",
    "get_live_allocation",
    "get_live_rules",
    "get_live_margin",
    "get_options_live",
    "get_broker_gateways",
}
# Not destructive: queue research jobs, or create / smoke-check a draft.
JOB_TOOLS = {
    "run_backtest",
    "run_lab",
    "run_ingest",
    "run_signal_ic",
    "run_factor_tearsheet",
    "run_options_backtest",
    "create_draft",
    "validate_draft",
    "backtest_draft",
    "lab_run_draft",
    "run_draft_backtest",
    "run_draft_lab",
    "run_sweep",
    "add_journal_note",
    "mark_notifications_read",
    "create_watchlist",
    "create_price_alert",
    "draft_order",
    "start_research",
    "create_screen",
    "retrain_models",
}
# Overwrite a draft's fields; no confirm (a draft is never traded).
EDIT_TOOLS = {
    "update_draft",
    "edit_journal_note",
    "cancel_job",
    "update_watchlist",
    "update_price_alert",
    "update_screen",
}
GUARDED_TOOLS = {
    "create_universe",
    "update_universe",
    "refresh_universe",
    "ensure_universe_data",
    "delete_universe",
    "import_index_history",
    "promote_strategy",
    "retire_strategy",
    "shadow_strategy",
    "run_tick",
    "register_draft",
    "enable_draft",
    "disable_draft",
    "sync_connection",
    "engage_kill_switch",
    "subscribe",
    "update_subscription",
    "delete_draft",
    "place_order",
    "change_order",
    "cancel_order",
    "delete_price_alert",
    "set_event_alerts",
    "save_screen_as_universe",
    "delete_screen",
    "swap_model_version",
    "reject_model_version",
}


@pytest.mark.anyio
async def test_tool_list_and_annotations(mcp):
    tools = {t.name: t for t in (await mcp.list_tools()).tools}
    assert set(tools) == READ_TOOLS | JOB_TOOLS | EDIT_TOOLS | GUARDED_TOOLS
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
        if name in EDIT_TOOLS:
            assert ann.destructive_hint is True and ann.idempotent_hint is True, name
        if name in GUARDED_TOOLS:
            assert ann.destructive_hint is True, name
            props = tool.input_schema["properties"]
            assert props["confirm"]["default"] is False, name
    assert tools["run_tick"].input_schema["properties"]["dry_run"]["default"] is True


@pytest.mark.anyio
async def test_no_tool_touches_broker_settings(mcp):
    for tool in (await mcp.list_tools()).tools:
        if any(word in tool.name for word in ("broker", "live", "config")):
            # only reads may mention the broker (get_broker shows its mode)
            assert tool.annotations.read_only_hint is True, tool.name
            assert tool.name in READ_TOOLS, tool.name


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
async def test_portfolio_tools_take_a_portfolio_id_that_must_be_yours(mcp):
    mine = await call(mcp, "get_portfolio", {"portfolio_id": "pf_default"})
    assert [p["ticker"] for p in mine["positions"]] == ["UP.US"]
    for tool in ("get_portfolio", "list_orders", "list_fills", "get_pnl"):
        text = await call_error(mcp, tool, {"portfolio_id": "pf_someone_else"})
        assert "not found" in text.lower(), (tool, text)
    totals = await call(mcp, "get_portfolio_totals")
    assert totals["portfolios"] >= 1 and "positions" not in totals


@pytest.mark.anyio
async def test_insight_tools(mcp):
    me = await call(mcp, "whoami")
    assert me["user_id"] == "usr_owner" and "read" in me["scopes"]
    insights = await call(mcp, "get_insights", {"benchmark": "UP.US"})
    assert insights["portfolio_id"] == "pf_default"
    assert {s["key"] for s in insights["allocation"]["ticker"]} >= {"UP.US"}
    assert insights["exposure"]["beta"] is not None
    agreement = await call(mcp, "get_strategy_agreement", {"portfolio_id": "pf_default"})
    assert agreement["holdings"][0]["opinions"][0]["stance"] == "agree"
    totals = await call(mcp, "get_insights_totals")
    assert totals["portfolios"] >= 1 and "UP.US" not in str(totals)
    for tool in ("get_insights", "get_strategy_agreement"):
        text = await call_error(mcp, tool, {"portfolio_id": "pf_someone_else"})
        assert "not found" in text.lower(), (tool, text)


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
async def test_statement_flags(mcp):
    flags = await call(mcp, "list_statement_flags", {"ticker": "UP.US", "severity": "error"})
    assert flags == {"items": [], "total": 0, "limit": 50, "offset": 0}


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


@pytest.mark.anyio
async def test_risk_policy_broker_sources_cost_models(mcp, settings):
    policy = await call(mcp, "get_risk_policy")
    assert policy["enabled"] is True
    broker = await call(mcp, "get_broker")
    assert broker["kind"] == "simulated"
    assert set(broker) == {"kind", "paper", "allow_live", "credentials_configured"}
    sources = (await call(mcp, "list_sources"))["items"]
    assert any(s["default"] for s in sources)
    presets = (await call(mcp, "list_cost_models"))["items"]
    assert {p["name"] for p in presets} == {"zero", "realistic"}


@pytest.mark.anyio
async def test_pnl_and_health_report(mcp):
    pnl = await call(mcp, "get_pnl")
    assert pnl["strategy_id"] is None and pnl["rows"]
    later = await call(mcp, "get_pnl", {"since": "2999-01-01"})
    assert later["rows"] == []
    report = await call(mcp, "get_health_report", {"tickers": ["UP.US", "DOWN.US"]})
    assert isinstance(report["healthy"], bool)
    assert report["checks"]
    stream = await call(mcp, "get_stream_status", {})
    assert stream["engines"] == []


@pytest.mark.anyio
async def test_shadow_reads(mcp, seeded):
    decisions = await call(mcp, "list_shadow_decisions", {"strategy_id": seeded["shadow_id"]})
    assert decisions["items"] == [] and decisions["total"] == 0
    summaries = (await call(mcp, "list_shadow_pnl", {"limit": 5}))["items"]
    assert all("cumulative_return" in row for row in summaries)
    series = await call(mcp, "get_shadow_pnl", {"strategy_id": seeded["shadow_id"]})
    assert series["strategy_id"] == seeded["shadow_id"]
    assert "nope" in await call_error(mcp, "get_shadow_pnl", {"strategy_id": "nope"})


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tool", "arg"),
    [
        ("get_strategy", "strategy_id"),
        ("get_shadow_pnl", "strategy_id"),
        ("get_tick", "tick_id"),
        ("get_job", "job_id"),
        ("wait_for_job", "job_id"),
    ],
)
async def test_read_ids_are_validated(mcp, tool, arg):
    assert "invalid id" in await call_error(mcp, tool, {arg: "../ticks#"})


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
    # the typed BacktestResult from /api/lab/backtests/{id}/result
    assert done["result"]["final_return"] == done["job"]["result"]["final_return"]
    assert done["result"]["equity"] and "profit_factor" in done["result"]
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
    assert done["result"]["verdict"] == done["job"]["result"]["verdict"]
    assert done["result"]["class_path"].endswith(":Momentum")


MOMENTUM = "stonks.strategies.examples.momentum:Momentum"
LAB_ARGS = {
    "class_path": MOMENTUM,
    "universe": ["UP.US", "DOWN.US"],
    "start": "2025-10-01",
    "end": "2026-04-01",
    "budget": 2,
}


@pytest.mark.anyio
async def test_lab_options_in_schemas(mcp):
    tools = {t.name: t for t in (await mcp.list_tools()).tools}
    backtest = tools["run_backtest"].input_schema
    assert "cost_model" in backtest["properties"]
    assert '"realistic"' in json.dumps(backtest)
    lab = tools["run_lab"].input_schema
    assert {"walk_forward", "mcpt"} <= set(lab["properties"])
    text = json.dumps(lab)
    for field in ("n_splits", "anchored", "n_permutations", "max_p_value", "walk_forward"):
        assert field in text, field


@pytest.mark.anyio
async def test_backtest_cost_model_preset(mcp):
    args = {
        "class_path": BAH,
        "params": {"ticker": "UP.US"},
        "universe": ["UP.US"],
        "start": "2025-10-01",
        "end": "2026-04-01",
    }
    job = await call(mcp, "run_backtest", {**args, "cost_model": "realistic"})
    done = await call(mcp, "wait_for_job", {"job_id": job["id"], "poll_seconds": 0.05})
    assert done["job"]["status"] == "succeeded", done["job"]["error"]
    assert job["params"]["cost_model"] == "realistic"
    err = await call_error(mcp, "run_backtest", {**args, "cost_model": "zero", "slippage_bps": 5})
    assert "422" in err and "not both" in err


@pytest.mark.anyio
async def test_lab_walk_forward_and_mcpt_options(mcp):
    job = await call(
        mcp,
        "run_lab",
        {
            **LAB_ARGS,
            "survival_tests": ["walk_forward", "permutation"],
            "walk_forward": {"n_splits": 2, "metric": "final_return"},
            "mcpt": {"n_permutations": 2, "seed": 3},
        },
    )
    assert job["params"]["walk_forward"]["n_splits"] == 2
    assert job["params"]["mcpt"]["n_permutations"] == 2
    done = await call(mcp, "wait_for_job", {"job_id": job["id"], "poll_seconds": 0.05})
    assert done["job"]["status"] == "succeeded", done["job"]["error"]
    tests = {r["test_id"] for r in done["result"]["survival_reports"]}
    assert tests == {"walk_forward", "mcpt"}  # the permutation test reports as "mcpt"


@pytest.mark.anyio
async def test_lab_registering_needs_confirm(mcp):
    args = {**LAB_ARGS, "survival_tests": ["oos"], "register_strategy": True}
    preview = await call(mcp, "run_lab", args)
    assert preview["preview"] is True and preview["applied"] is False
    assert preview["request"]["register_strategy"] is True
    assert (await call(mcp, "list_jobs"))["total"] == 0

    out = await call(mcp, "run_lab", {**args, "confirm": True})
    done = await call(mcp, "wait_for_job", {"job_id": out["job"]["id"], "poll_seconds": 0.05})
    assert done["job"]["status"] == "succeeded", done["job"]["error"]
    assert done["result"]["registered_strategy_id"]


@pytest.mark.anyio
async def test_lab_options_need_their_test(mcp):
    err = await call_error(
        mcp, "run_lab", {**LAB_ARGS, "survival_tests": ["oos"], "mcpt": {"n_permutations": 2}}
    )
    assert "422" in err and "permutation" in err


@pytest.mark.anyio
async def test_ingest_job(mcp):
    job = await call(
        mcp, "run_ingest", {"kind": "prices", "source": "eodhd", "tickers": ["NEW.US"]}
    )
    done = await call(mcp, "wait_for_job", {"job_id": job["id"], "poll_seconds": 0.05})
    assert done["job"]["status"] == "succeeded", done["job"]["error"]
    assert done["result"]["kind"] == "prices" and done["result"]["tickers_ok"] == 1
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

    # Applying goes through the governed service (BL-24): promote needs a
    # passing go-live check, retire a reason; neither is available here.
    err = await call_error(mcp, tool, {"strategy_id": sid, "confirm": True})
    assert "go-live" in err or "reason" in err
    assert (await call(mcp, "get_strategy", {"strategy_id": sid}))["status"] == "shadow"


@pytest.mark.anyio
async def test_shadow_strategy_guarded(mcp, seeded):
    sid = seeded["active_id"]
    preview = await call(mcp, "shadow_strategy", {"strategy_id": sid})
    assert preview["new_status"] == "shadow"
    assert (await call(mcp, "get_strategy", {"strategy_id": sid}))["status"] == "active"
    err = await call_error(mcp, "shadow_strategy", {"strategy_id": sid, "confirm": True})
    assert "reason" in err
    assert (await call(mcp, "get_strategy", {"strategy_id": sid}))["status"] == "active"


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
    assert done["result"]["dry_run"] is True and done["result"]["tick_id"]


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


def _job_transport(job: dict, seen: list[str]) -> httpx2.MockTransport:
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/result"):
            return httpx2.Response(200, json={"typed": True})
        return httpx2.Response(200, json=job)

    return httpx2.MockTransport(handler)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("kind", "status", "result_path", "expected"),
    [
        ("backtest", "succeeded", "/api/lab/backtests/j1/result", {"typed": True}),
        ("lab_run", "succeeded", "/api/lab/runs/j1/result", {"typed": True}),
        ("ingest", "succeeded", "/api/ingest/jobs/j1/result", {"typed": True}),
        ("tick", "succeeded", "/api/ticks/jobs/j1/result", {"typed": True}),
        # no typed route for studio jobs: the job's own result
        ("studio_backtest", "succeeded", None, {"raw": 1}),
        # only a succeeded job has a result to fetch
        ("backtest", "failed", None, None),
    ],
)
async def test_wait_for_job_fetches_typed_result(kind, status, result_path, expected):
    seen: list[str] = []
    job = {"id": "j1", "kind": kind, "status": status, "result": {"raw": 1}, "error": None}
    if status != "succeeded":
        job["result"] = None
    api = ApiClient(BASE, token=API_TOKEN, transport=_job_transport(job, seen))
    async with Client(build_server(api)) as c:
        done = await call(c, "wait_for_job", {"job_id": "j1"})
    assert done["result"] == expected
    assert [p for p in seen if p.endswith("/result")] == ([result_path] if result_path else [])


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
                ("get_broker", {}),
                ("list_sources", {}),
                ("get_health_report", {}),
                ("create_draft", {"name": "c", "kind": "code", "source_code": "x = 1"}),
                ("update_draft", {"draft_id": "d1", "name": "x"}),
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
