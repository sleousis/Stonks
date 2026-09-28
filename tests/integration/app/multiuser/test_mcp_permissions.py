"""MCP per user (roadmap S10), end to end: MCP client -> MCP server ->
ApiClient with one person's token -> the FastAPI app.

- Parity: every MCP tool is called as several principals (viewer, trader,
  admin with and without the admin scope). Each REST request it makes must
  be refused exactly when the policy refuses that route's permission to
  the token's principal, and the tool must fail with a clear message then.
  A new tool without a row here fails the test.
- Step-up: no tool reaches a step-up route; those are done in the web app.
- Tenant isolation: Alice's MCP server, handed Bob's ids, never shows Bob's
  rows.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import anyio
import httpx2
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from mcp import Client

from stonks.accounts import PortfolioRepository, Role
from stonks.accounts.scope import Scope
from stonks.api import create_app
from stonks.api.deps import route_permissions
from stonks.app.connections import ConnectionsAppService, ConnectWithKeysRequest
from stonks.app.context import AppContext
from stonks.app.services import Services
from stonks.app.universes import UniverseCreate
from stonks.auth import AuthService, Permission, Principal
from stonks.auth.policy import POLICY, allowed
from stonks.connections.providers import fake
from stonks.connections.ratelimit import reset_limiters
from stonks.connections.settings import ConnectionsConfig
from stonks.mcp.client import ApiClient
from stonks.mcp.server import build_server
from stonks.store.state import SqliteState
from tests.integration.app.test_api import REMOTE
from tests.integration.app.test_mcp_server import (
    EDIT_TOOLS,
    GUARDED_TOOLS,
    JOB_TOOLS,
    READ_TOOLS,
)
from tests.integration.auth.helpers import add_user, session_principal

BASE = "http://127.0.0.1:8000"
_HOP = {"content-length", "content-encoding", "transfer-encoding"}
PUBLIC_PATHS = {"/api/health"}


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
def app(settings, seeded, fake_source, auth, box):
    settings.api.allowed_hosts = ["testserver"]
    ctx = AppContext(settings, source_factory=lambda: fake_source)
    svc = Services.create(ctx)
    svc.connections = ConnectionsAppService(
        ctx, config=ConnectionsConfig(enabled_providers=("fake",)), box=box
    )
    application = create_app(settings, services=svc)
    application.state.auth = auth
    return application


@pytest.fixture
def tc(app):
    with TestClient(app, client=REMOTE, base_url="https://testserver") as c:
        yield c


# ---- recording bridge ----------------------------------------------------------


@dataclass
class Seen:
    method: str
    path: str
    status: int


@dataclass
class Recorder:
    requests: list[Seen] = field(default_factory=list)

    def transport(self, tc: TestClient) -> httpx2.MockTransport:
        async def handler(request: httpx2.Request) -> httpx2.Response:
            def forward() -> httpx2.Response:
                headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}
                target = request.url.raw_path.decode()
                resp = tc.request(request.method, target, headers=headers, content=request.content)
                self.requests.append(Seen(request.method, request.url.path, resp.status_code))
                out = [(k, v) for k, v in resp.headers.items() if k.lower() not in _HOP]
                return httpx2.Response(resp.status_code, headers=out, content=resp.content)

            return await anyio.to_thread.run_sync(forward)

        return httpx2.MockTransport(handler)


def _flatten(routes: Any) -> list[APIRoute]:
    out: list[APIRoute] = []
    for r in routes:
        if isinstance(r, APIRoute):
            out.append(r)
        elif hasattr(r, "original_router"):
            out.extend(_flatten(r.original_router.routes))
    return out


class Routes:
    """Maps a concrete request to its route template and permission."""

    def __init__(self, app) -> None:
        self._routes = _flatten(app.routes)
        self._perms = {(m, p): perm for m, p, perm in route_permissions(app.routes)}

    def template(self, method: str, path: str) -> str:
        for r in self._routes:
            if method in r.methods and r.path_regex.match(path):
                return r.path
        raise AssertionError(f"no route for {method} {path}")

    def permission(self, method: str, template: str) -> Permission | None:
        perm = self._perms.get((method, template))
        if perm is not None:
            return perm
        if template in PUBLIC_PATHS:
            return None
        assert method == "GET", f"unsafe route without a permission: {method} {template}"
        return Permission.READ

    def step_up_routes(self) -> set[tuple[str, str]]:
        return {(m, p) for (m, p), perm in self._perms.items() if POLICY[perm].step_up}


# ---- principals and their own rows -----------------------------------------------


@dataclass
class Person:
    name: str
    user_id: str
    role: Role
    scopes: list[str]
    token: str
    ids: dict[str, str]

    @property
    def principal(self) -> Principal:
        return Principal.create(
            user_id=self.user_id,
            kind="human",
            role=self.role,
            scopes=self.scopes,
            mfa_fresh=False,
            via="token",
        )


def _seed_own_rows(app, path, user_id: str, role: Role, tag: str) -> dict[str, str]:
    services = app.state.services
    with SqliteState(path) as state:
        pid = (
            PortfolioRepository(state)
            .create(Scope(user_id=user_id, role=role), name=f"{tag}BOOK")
            .id
        )
        now = datetime.now(UTC).isoformat()
        state.execute(
            "INSERT INTO strategy_drafts (id, name, kind, spec_json, status, created_at,"
            " updated_at, owner_id) VALUES (?, ?, 'rule', '{}', 'draft', ?, ?, ?)",
            [f"draft_{tag}", f"{tag}DRAFT", now, now, user_id],
        )
        state.execute(
            "INSERT INTO subscriptions (id, user_id, strategy_id, portfolio_id, mode,"
            " created_at, updated_at) VALUES (?, ?, 'bah_shadow', ?, 'paper', ?, ?)",
            [f"sub_{tag}", user_id, pid, now, now],
        )
        state.execute(
            "INSERT INTO watchlists (id, owner_id, name, tickers_json, created_at, updated_at)"
            " VALUES (?, ?, ?, '[\"UP.US\"]', ?, ?)",
            [f"wl_{tag.lower()}", user_id, f"{tag}LIST", now, now],
        )
        state.execute(
            "INSERT INTO screens (id, owner_id, name, spec_json, created_at, updated_at)"
            " VALUES (?, ?, ?, '{}', ?, ?)",
            [f"scr_{tag.lower()}", user_id, f"{tag}SCREEN", now, now],
        )
    job = services.runner.store.create("backtest", {"marker": f"{tag}JOB"}, owner_id=user_id)
    conn = services.connections.connect_with_keys(
        session_principal(user_id, role).scope,
        ConnectWithKeysRequest(provider="fake", fields={"token": f"{tag}-secret"}, label=tag),
    )
    return {
        "portfolio": pid,
        "draft": f"draft_{tag}",
        "subscription": f"sub_{tag}",
        "job": job.id,
        "connection": conn.id,
        "watchlist": f"wl_{tag.lower()}",
        "screen": f"scr_{tag.lower()}",
    }


@pytest.fixture
def people(app, settings, auth: AuthService) -> dict[str, Person]:
    path = settings.state.path
    app.state.services.universes.create(
        UniverseCreate(id="u_perm", kind="list", spec={"tickers": ["UP.US"]})
    )
    out: dict[str, Person] = {}
    users: dict[str, str] = {}
    for name, role, scopes, owner in [
        ("vic", Role.VIEWER, ["read"], "vic"),
        ("alice", Role.TRADER, ["read", "trade", "lab"], "alice"),
        ("alice_read", Role.TRADER, ["read"], "alice"),
        ("ada_nonadmin", Role.ADMIN, ["read", "trade", "lab"], "ada"),
        ("ada", Role.ADMIN, ["read", "trade", "lab", "admin"], "ada"),
        ("bob", Role.TRADER, ["read", "trade", "lab"], "bob"),
    ]:
        if owner not in users:
            users[owner] = add_user(path, f"{owner}@example.com", role)
        uid = users[owner]
        _, token = auth.create_token(session_principal(uid, role), name=name, scopes=scopes)
        out[name] = Person(name, uid, role, scopes, token, {})
    for owner, uid in users.items():
        ids = _seed_own_rows(app, path, uid, out[owner].role, owner.upper())
        for p in out.values():
            if p.user_id == uid:
                p.ids = ids
    return out


# ---- the tool table ------------------------------------------------------------

Args = Callable[[dict[str, str]], dict[str, Any]]


@dataclass(frozen=True)
class Case:
    route: tuple[str, str]
    args: Args = lambda ids: {}


def _c(method: str, route: str, args: Args | None = None) -> Case:
    return Case((method, route), args or (lambda ids: {}))


_WINDOW = {"start": "2026-03-01", "end": "2026-01-01"}  # inverted: 422 after the permission
_RULE = {"version": 1}

CASES: dict[str, Case] = {
    # reads
    "health": _c("GET", "/api/health"),
    "get_api_health": _c("GET", "/api/health"),
    "whoami": _c("GET", "/api/auth/me"),
    "get_portfolio": _c("GET", "/api/portfolio", lambda i: {"portfolio_id": i["portfolio"]}),
    "get_portfolio_totals": _c("GET", "/api/portfolio/totals"),
    "list_portfolio_snapshots": _c(
        "GET", "/api/portfolio/snapshots", lambda i: {"portfolio_id": i["portfolio"]}
    ),
    "list_strategies": _c("GET", "/api/strategies"),
    "get_strategy": _c(
        "GET", "/api/strategies/{strategy_id}", lambda i: {"strategy_id": "bah_active"}
    ),
    "get_strategy_history": _c(
        "GET", "/api/strategies/{strategy_id}/history", lambda i: {"strategy_id": "bah_active"}
    ),
    "search_instruments": _c("GET", "/api/market/instruments"),
    "get_bars": _c("GET", "/api/market/bars", lambda i: {"ticker": "UP.US"}),
    "get_coverage": _c("GET", "/api/market/coverage"),
    "list_orders": _c("GET", "/api/orders", lambda i: {"portfolio_id": i["portfolio"]}),
    "list_fills": _c("GET", "/api/orders/fills", lambda i: {"portfolio_id": i["portfolio"]}),
    "list_ticks": _c("GET", "/api/ticks"),
    "get_tick": _c("GET", "/api/ticks/{tick_id}", lambda i: {"tick_id": i["tick"]}),
    "list_ingest_runs": _c("GET", "/api/ingest/runs"),
    "list_statement_flags": _c("GET", "/api/statements/flags"),
    "get_catalog": _c("GET", "/api/catalog/asset-classes"),
    "list_jobs": _c("GET", "/api/jobs"),
    "get_job": _c("GET", "/api/jobs/{job_id}", lambda i: {"job_id": i["job"]}),
    "wait_for_job": _c(
        "GET",
        "/api/jobs/{job_id}",
        lambda i: {"job_id": i["job"], "timeout_seconds": 0.05, "poll_seconds": 0.05},
    ),
    "get_risk_policy": _c("GET", "/api/risk/policy"),
    "get_pnl": _c("GET", "/api/pnl", lambda i: {"portfolio_id": i["portfolio"]}),
    "live_risk": _c("GET", "/api/risk/live", lambda i: {"portfolio_id": i["portfolio"]}),
    "risk_snapshots": _c("GET", "/api/risk/snapshots", lambda i: {"portfolio_id": i["portfolio"]}),
    "get_live_risk": _c("GET", "/api/risk/live", lambda i: {"portfolio_id": i["portfolio"]}),
    "list_risk_snapshots": _c(
        "GET", "/api/risk/snapshots", lambda i: {"portfolio_id": i["portfolio"]}
    ),
    "list_intraday_snapshots": _c(
        "GET", "/api/risk/intraday", lambda i: {"portfolio_id": i["portfolio"]}
    ),
    "get_golive_report": _c(
        "GET", "/api/strategies/{strategy_id}/golive", lambda i: {"strategy_id": "bah_active"}
    ),
    "get_schedule": _c("GET", "/api/schedule"),
    "list_ledger_runs": _c("GET", "/api/lab/ledger"),
    "get_ledger_run": _c("GET", "/api/lab/ledger/{run_id}", lambda i: {"run_id": "lab_missing"}),
    "list_research_sessions": _c("GET", "/api/assistant/research"),
    "get_research_session": _c(
        "GET", "/api/assistant/research/{session_id}", lambda i: {"session_id": "rs_missing"}
    ),
    "list_alerts": _c("GET", "/api/alerts"),
    "list_notifications": _c("GET", "/api/notifications"),
    "mark_notifications_read": _c("POST", "/api/notifications/read"),
    "get_notification_preferences": _c("GET", "/api/notifications/preferences"),
    "set_event_alerts": _c(
        "PUT",
        "/api/notifications/preferences",
        lambda i: {"dividends": False, "confirm": True},
    ),
    "list_survival_tests": _c("GET", "/api/lab/survival-tests"),
    "list_survival_presets": _c("GET", "/api/lab/survival-presets"),
    "get_studio_capabilities": _c("GET", "/api/studio/capabilities"),
    "list_shadow_decisions": _c("GET", "/api/shadow/decisions"),
    "list_shadow_pnl": _c("GET", "/api/shadow/pnl"),
    "get_shadow_pnl": _c(
        "GET", "/api/shadow/strategies/{strategy_id}/pnl", lambda i: {"strategy_id": "bah_shadow"}
    ),
    "get_health_report": _c("GET", "/api/health/report"),
    "get_stream_status": _c("GET", "/api/stream/status"),
    "get_broker": _c("GET", "/api/brokers"),
    "list_sources": _c("GET", "/api/sources"),
    "list_cost_models": _c("GET", "/api/lab/cost-models"),
    "list_studio_templates": _c("GET", "/api/studio/templates"),
    "get_rule_schema": _c("GET", "/api/studio/schema"),
    "validate_rule_spec": _c("POST", "/api/studio/spec/validate", lambda i: {"spec": _RULE}),
    "list_drafts": _c("GET", "/api/studio/drafts"),
    "get_draft": _c("GET", "/api/studio/drafts/{draft_id}", lambda i: {"draft_id": i["draft"]}),
    "list_connections": _c("GET", "/api/connections"),
    "get_connection_accounts": _c(
        "GET",
        "/api/connections/{connection_id}/accounts",
        lambda i: {"connection_id": i["connection"]},
    ),
    "list_halts": _c("GET", "/api/halts"),
    "list_reconcile_reports": _c("GET", "/api/reconcile/reports"),
    "get_reconcile_report": _c(
        "GET", "/api/reconcile/reports/{report_id}", lambda i: {"report_id": "rec_nope"}
    ),
    "tca_summary": _c("GET", "/api/tca/summary", lambda i: {"portfolio_id": i["portfolio"]}),
    "trade_journal": _c("GET", "/api/tca/journal", lambda i: {"portfolio_id": i["portfolio"]}),
    "order_tca": _c("GET", "/api/tca/orders/{client_id}", lambda i: {"client_id": "nope"}),
    "get_tca_summary": _c("GET", "/api/tca/summary", lambda i: {"portfolio_id": i["portfolio"]}),
    "list_trade_journal": _c("GET", "/api/tca/journal", lambda i: {"portfolio_id": i["portfolio"]}),
    "get_order_tca": _c("GET", "/api/tca/orders/{client_id}", lambda i: {"client_id": "nope"}),
    "add_journal_note": _c(
        "POST",
        "/api/tca/orders/{client_id}/notes",
        lambda i: {"client_id": "nope", "note": "why"},
    ),
    "edit_journal_note": _c(
        "PUT", "/api/tca/notes/{note_id}", lambda i: {"note_id": 999999, "note": "why"}
    ),
    "list_portfolios": _c("GET", "/api/portfolios"),
    "list_trading_modes": _c("GET", "/api/portfolios/trading-modes"),
    "list_subscriptions": _c("GET", "/api/subscriptions"),
    "list_universes": _c("GET", "/api/universes"),
    "get_universe": _c("GET", "/api/universes/{universe_id}", lambda i: {"universe_id": "u_perm"}),
    "get_universe_history": _c(
        "GET", "/api/universes/{universe_id}/history", lambda i: {"universe_id": "u_perm"}
    ),
    "list_universe_exchanges": _c("GET", "/api/universes/exchanges"),
    "update_universe": _c(
        "PUT",
        "/api/universes/{universe_id}",
        lambda i: {"universe_id": "u_perm", "kind": "list", "spec": {}, "confirm": True},
    ),
    "get_universe_members": _c(
        "GET", "/api/universes/{universe_id}/members", lambda i: {"universe_id": "u_perm"}
    ),
    "get_insights": _c("GET", "/api/insights", lambda i: {"portfolio_id": i["portfolio"]}),
    "get_strategy_agreement": _c(
        "GET", "/api/insights/agreement", lambda i: {"portfolio_id": i["portfolio"]}
    ),
    "get_insights_totals": _c("GET", "/api/insights/totals"),
    "get_chart": _c(
        "GET",
        "/api/charts/{ticker}",
        lambda i: {"ticker": "UP.US", "portfolio_id": i["portfolio"]},
    ),
    "compare_tickers": _c(
        "GET", "/api/charts/compare", lambda i: {"tickers": ["UP.US", "DOWN.US"]}
    ),
    "get_leaderboard": _c("GET", "/api/strategies/leaderboard"),
    "get_tear_sheet": _c(
        "GET", "/api/strategies/{strategy_id}/tearsheet", lambda i: {"strategy_id": "bah_active"}
    ),
    "list_watchlists": _c("GET", "/api/watchlists"),
    "get_watchlist": _c(
        "GET", "/api/watchlists/{watchlist_id}", lambda i: {"watchlist_id": i["watchlist"]}
    ),
    "get_my_risk_limits": _c("GET", "/api/risk/limits"),
    "get_calendar": _c("GET", "/api/calendars", lambda i: {"portfolio_id": i["portfolio"]}),
    "get_news": _c("GET", "/api/calendars/news", lambda i: {"portfolio_id": i["portfolio"]}),
    "get_earnings_warnings": _c(
        "GET", "/api/calendars/earnings-warnings", lambda i: {"tickers": ["UP.US"]}
    ),
    "list_event_alert_kinds": _c("GET", "/api/calendars/alert-kinds"),
    "list_screen_metrics": _c("GET", "/api/screener/metrics"),
    "run_screen": _c("POST", "/api/screener/run", lambda i: {"screen_id": i["screen"]}),
    "list_screens": _c("GET", "/api/screener/screens"),
    "get_screen": _c(
        "GET", "/api/screener/screens/{screen_id}", lambda i: {"screen_id": i["screen"]}
    ),
    "create_screen": _c(
        "POST", "/api/screener/screens", lambda i: {"name": "x", "spec": {"limit": 0}}
    ),
    "update_screen": _c(
        "PATCH",
        "/api/screener/screens/{screen_id}",
        lambda i: {"screen_id": i["screen"], "spec": {"limit": 3}},
    ),
    "delete_screen": _c(
        "DELETE",
        "/api/screener/screens/{screen_id}",
        lambda i: {"screen_id": i["screen"], "confirm": True},
    ),
    "save_screen_as_universe": _c(
        "POST",
        "/api/screener/universes",
        lambda i: {"universe_id": "BAD ID", "spec": {}, "confirm": True},
    ),
    "create_watchlist": _c("POST", "/api/watchlists", lambda i: {"name": "x", "tickers": ["=bad"]}),
    "update_watchlist": _c(
        "PATCH",
        "/api/watchlists/{watchlist_id}",
        lambda i: {"watchlist_id": i["watchlist"], "tickers": ["UP.US", "DOWN.US"]},
    ),
    # research jobs (an inverted window: the API answers 422 once the permission passed)
    "run_backtest": _c(
        "POST",
        "/api/lab/backtests",
        lambda i: {"universe": ["UP.US"], "strategy_id": "bah_active"} | _WINDOW,
    ),
    "run_lab": _c(
        "POST",
        "/api/lab/runs",
        lambda i: {"universe": ["UP.US"], "class_path": "x.y:Z"} | _WINDOW,
    ),
    "run_signal_ic": _c(
        "POST",
        "/api/lab/signal-ic",
        lambda i: {"universe": ["UP.US"], "strategy_id": "bah_active"} | _WINDOW,
    ),
    "run_factor_tearsheet": _c(
        "POST",
        "/api/factors/tearsheets",
        lambda i: {"factor": "KMID", "universe": ["UP.US"]} | _WINDOW,
    ),
    "run_options_backtest": _c(
        "POST",
        "/api/options/backtests",
        lambda i: {"strategy": "covered_call", "underlyings": ["UP.US"]} | _WINDOW,
    ),
    "list_option_underlyings": _c("GET", "/api/options/underlyings"),
    "get_option_chain": _c(
        "GET", "/api/options/chains/{underlying}", lambda i: {"underlying": "UP.US"}
    ),
    "list_option_strategies": _c("GET", "/api/options/strategies"),
    "list_option_structures": _c("GET", "/api/options/structures"),
    "get_option_payoff": _c(
        "POST",
        "/api/options/payoff",
        lambda i: {"underlying": "UP.US", "structure": "long_call"},
    ),
    "list_factors": _c("GET", "/api/factors"),
    "get_factor": _c("GET", "/api/factors/{factor_id}", lambda i: {"factor_id": "KMID"}),
    "check_factor_expression": _c(
        "POST", "/api/factors/check", lambda i: {"expression": "$close/$open"}
    ),
    "get_factor_values": _c(
        "POST",
        "/api/factors/values",
        lambda i: {"factor": "KMID", "universe": ["UP.US"], "as_of": "2026-04-01"},
    ),
    "run_ingest": _c(
        "POST",
        "/api/ingest/runs",
        lambda i: {
            "kind": "prices",
            "tickers": ["NEW.US"],
            "since": "2026-04-03",
            "until": "2026-04-01",
        },
    ),
    "create_draft": _c("POST", "/api/studio/drafts", lambda i: {"name": "x", "spec": {}}),
    "validate_draft": _c(
        "POST", "/api/studio/drafts/{draft_id}/validate", lambda i: {"draft_id": i["draft"]}
    ),
    "backtest_draft": _c(
        "POST",
        "/api/studio/drafts/{draft_id}/backtests",
        lambda i: {"draft_id": i["draft"], "universe": ["UP.US"]} | _WINDOW,
    ),
    "lab_run_draft": _c(
        "POST",
        "/api/studio/drafts/{draft_id}/lab-runs",
        lambda i: {"draft_id": i["draft"], "universe": ["UP.US"]} | _WINDOW,
    ),
    "run_draft_backtest": _c(
        "POST",
        "/api/studio/drafts/{draft_id}/backtests",
        lambda i: {"draft_id": i["draft"], "universe": ["UP.US"]} | _WINDOW,
    ),
    "run_draft_lab": _c(
        "POST",
        "/api/studio/drafts/{draft_id}/lab-runs",
        lambda i: {"draft_id": i["draft"], "universe": ["UP.US"]} | _WINDOW,
    ),
    "run_sweep": _c("POST", "/api/lab/sweeps", lambda i: {"universe": ["UP.US"]} | _WINDOW),
    # the research loop is off here: 503 once the permission passed
    "start_research": _c(
        "POST",
        "/api/assistant/research",
        lambda i: {"goal": "find an edge in these names", "universe": ["UP.US"]},
    ),
    "cancel_job": _c("POST", "/api/jobs/{job_id}/cancel", lambda i: {"job_id": i["job"]}),
    "update_draft": _c(
        "PATCH", "/api/studio/drafts/{draft_id}", lambda i: {"draft_id": i["draft"], "name": "y"}
    ),
    # model versions (roadmap 22.6)
    "list_model_versions": _c(
        "GET", "/api/strategies/{strategy_id}/versions", lambda i: {"strategy_id": "bah_active"}
    ),
    "get_model_version_history": _c(
        "GET",
        "/api/strategies/{strategy_id}/versions/history",
        lambda i: {"strategy_id": "bah_active"},
    ),
    "list_model_candidates": _c("GET", "/api/model-versions/candidates"),
    "check_model_swap": _c(
        "GET",
        "/api/strategies/{strategy_id}/versions/{version}/check",
        lambda i: {"strategy_id": "bah_active", "version": 1},
    ),
    "retrain_models": _c(
        "POST",
        "/api/model-versions/retrain",
        lambda i: {"strategy_ids": ["bah_active"], "confirm": True},
    ),
    "swap_model_version": _c(
        "POST",
        "/api/strategies/{strategy_id}/versions/{version}/swap",
        lambda i: {"strategy_id": "bah_active", "version": 1, "confirm": True},
    ),
    "reject_model_version": _c(
        "POST",
        "/api/strategies/{strategy_id}/versions/{version}/reject",
        lambda i: {"strategy_id": "bah_active", "version": 1, "reason": "x", "confirm": True},
    ),
    # guarded writes, confirmed
    "promote_strategy": _c(
        "POST",
        "/api/strategies/{strategy_id}/promote",
        lambda i: {"strategy_id": "no_such_strategy", "confirm": True},
    ),
    "shadow_strategy": _c(
        "POST",
        "/api/strategies/{strategy_id}/shadow",
        lambda i: {"strategy_id": "bah_shadow", "reason": "keep it in shadow", "confirm": True},
    ),
    "retire_strategy": _c(
        "POST",
        "/api/strategies/{strategy_id}/retire",
        lambda i: {"strategy_id": "no_such_strategy", "reason": "x", "confirm": True},
    ),
    "run_tick": _c("POST", "/api/ticks", lambda i: {"as_of": "2030-01-01", "confirm": True}),
    "register_draft": _c(
        "POST",
        "/api/studio/drafts/{draft_id}/register",
        lambda i: {"draft_id": i["draft"], "confirm": True},
    ),
    "enable_draft": _c(
        "POST",
        "/api/studio/drafts/{draft_id}/enable",
        lambda i: {"draft_id": i["draft"], "confirm": True},
    ),
    "disable_draft": _c(
        "POST",
        "/api/studio/drafts/{draft_id}/disable",
        lambda i: {"draft_id": i["draft"], "confirm": True},
    ),
    "sync_connection": _c(
        "POST",
        "/api/connections/{connection_id}/sync",
        lambda i: {"connection_id": i["connection"], "confirm": True},
    ),
    "engage_kill_switch": _c(
        "POST",
        "/api/halts/kill",
        lambda i: {
            "scope": "portfolio",
            "portfolio_id": i["portfolio"],
            "reason": "t",
            "confirm": True,
        },
    ),
    "subscribe": _c(
        "POST",
        "/api/subscriptions",
        lambda i: {"strategy_id": "no_such_strategy", "confirm": True},
    ),
    "update_subscription": _c(
        "PATCH",
        "/api/subscriptions/{subscription_id}",
        lambda i: {"subscription_id": i["subscription"], "enabled": True, "confirm": True},
    ),
    "create_universe": _c(
        "POST",
        "/api/universes",
        lambda i: {"universe_id": "bad id!", "kind": "list", "confirm": True},
    ),
    "refresh_universe": _c(
        "POST",
        "/api/universes/{universe_id}/refresh",
        lambda i: {"universe_id": "u_perm", "confirm": True},
    ),
    "ensure_universe_data": _c(
        "POST",
        "/api/universes/{universe_id}/ensure",
        lambda i: {"universe_id": "u_perm", "confirm": True} | _WINDOW,
    ),
    "import_index_history": _c(
        "POST",
        "/api/universes/index-history",
        lambda i: {"index_id": "BAD ID", "content": "", "confirm": True},
    ),
    "delete_universe": _c(
        "DELETE",
        "/api/universes/{universe_id}",
        lambda i: {"universe_id": "u_missing", "confirm": True},
    ),
    "delete_draft": _c(
        "DELETE",
        "/api/studio/drafts/{draft_id}",
        lambda i: {"draft_id": i["draft"], "confirm": True},
    ),
    # price alerts (roadmap 20.2) and currency and tax reads (20.5)
    "list_price_alerts": _c("GET", "/api/price-alerts"),
    "list_price_alert_events": _c("GET", "/api/price-alerts/events"),
    "create_price_alert": _c(
        "POST",
        "/api/price-alerts",
        lambda i: {"condition": "crosses_above", "ticker": "UP.US", "level": 100.0},
    ),
    "update_price_alert": _c(
        "PATCH", "/api/price-alerts/{alert_id}", lambda i: {"alert_id": "pal_none", "level": 5.0}
    ),
    "delete_price_alert": _c(
        "DELETE",
        "/api/price-alerts/{alert_id}",
        lambda i: {"alert_id": "pal_none", "confirm": True},
    ),
    "get_tax_settings": _c("GET", "/api/tax/settings", lambda i: {"portfolio_id": i["portfolio"]}),
    "list_tax_lot_picks": _c(
        "GET", "/api/tax/lots/picks", lambda i: {"portfolio_id": i["portfolio"]}
    ),
    "get_fx_rate": _c("GET", "/api/fx/rate", lambda i: {"base": "EUR", "quote": "USD"}),
    "list_cash_flows": _c(
        "GET",
        "/api/portfolios/{portfolio_id}/cash-flows",
        lambda i: {"portfolio_id": i["portfolio"]},
    ),
    # order drafts (roadmap 20.4): a stale price answers 409 once permitted
    "list_order_drafts": _c("GET", "/api/orders/drafts"),
    # order tickets (roadmap 19.8): read-only over MCP
    "list_tickets": _c("GET", "/api/tickets"),
    "get_ticket": _c("GET", "/api/tickets/{ticket_id}", lambda i: {"ticket_id": "tkt_none"}),
    # live stages and settings (roadmap 19.9): read-only over MCP
    "get_live_stage": _c(
        "GET",
        "/api/portfolios/{portfolio_id}/live/stage",
        lambda i: {"portfolio_id": i["portfolio"]},
    ),
    "get_live_gate_report": _c(
        "GET",
        "/api/portfolios/{portfolio_id}/live/gate-report",
        lambda i: {"portfolio_id": i["portfolio"]},
    ),
    "get_live_allocation": _c(
        "GET",
        "/api/portfolios/{portfolio_id}/live/allocation",
        lambda i: {"portfolio_id": i["portfolio"]},
    ),
    "get_live_rules": _c(
        "GET",
        "/api/portfolios/{portfolio_id}/live/rules",
        lambda i: {"portfolio_id": i["portfolio"]},
    ),
    "get_live_margin": _c(
        "GET",
        "/api/portfolios/{portfolio_id}/live/margin",
        lambda i: {"portfolio_id": i["portfolio"]},
    ),
    "get_options_live": _c(
        "GET",
        "/api/portfolios/{portfolio_id}/live/options",
        lambda i: {"portfolio_id": i["portfolio"]},
    ),
    "get_broker_gateways": _c("GET", "/api/brokers/gateways"),
    "draft_order": _c(
        "POST",
        "/api/orders/drafts",
        lambda i: {
            "portfolio_id": i["portfolio"],
            "ticker": "UP.US",
            "side": "buy",
            "quantity": 1,
            "reason": "by hand",
            "retry_key": "perm-1",
        },
    ),
    # manual orders (roadmap 20.1): a stale price answers 409 once permitted
    "place_order": _c(
        "POST",
        "/api/orders/manual",
        lambda i: {
            "portfolio_id": i["portfolio"],
            "ticker": "UP.US",
            "side": "buy",
            "quantity": 1,
            "reason": "by hand",
            "confirm": True,
        },
    ),
    "change_order": _c(
        "POST",
        "/api/orders/{client_id}/change",
        lambda i: {
            "client_id": "manual:none:x",
            "portfolio_id": i["portfolio"],
            "quantity": 2,
            "reason": "by hand",
            "confirm": True,
        },
    ),
    "cancel_order": _c(
        "POST",
        "/api/orders/{client_id}/cancel",
        lambda i: {
            "client_id": "manual:none:x",
            "portfolio_id": i["portfolio"],
            "reason": "by hand",
            "confirm": True,
        },
    ),
}

#: Tools whose preview reads the target first; a missing id stops them there.
_PREVIEW_READ_404 = {"delete_universe", "promote_strategy", "retire_strategy", "delete_draft"}


def test_every_tool_has_a_case():
    assert set(CASES) == READ_TOOLS | JOB_TOOLS | EDIT_TOOLS | GUARDED_TOOLS


async def _call(
    app, tc: TestClient, token: str, name: str, args: dict[str, Any]
) -> tuple[Any, list[Seen]]:
    recorder = Recorder()
    api = ApiClient(BASE, token=token, transport=recorder.transport(tc))
    async with Client(build_server(api, max_wait_seconds=5)) as client:
        result = await client.call_tool(name, args)
    return result, recorder.requests


PARITY_PEOPLE = ["vic", "alice", "alice_read", "ada_nonadmin", "ada"]


@pytest.mark.anyio
async def test_every_tool_enforces_its_routes_permission(app, tc, people, seeded):
    routes = Routes(app)
    step_up = routes.step_up_routes()
    reached: dict[str, set[str]] = {}
    failures: list[str] = []
    denials = 0
    for name, case in CASES.items():
        for who in PARITY_PEOPLE:
            person = people[who]
            ids = person.ids | {"tick": seeded["tick_id"]}
            result, seen = await _call(app, tc, person.token, name, case.args(ids))
            principal = person.principal
            denied = False
            for req in seen:
                template = routes.template(req.method, req.path)
                assert (req.method, template) not in step_up, (name, template)
                if template == case.route[1] and req.method == case.route[0]:
                    reached.setdefault(name, set()).add(who)
                perm = routes.permission(req.method, template)
                ok = perm is None or allowed(principal, perm)
                if ok and req.status in (401, 403):
                    failures.append(f"{name} as {who}: {req.method} {template} got {req.status}")
                if not ok:
                    denied = True
                    if req.status != 403:
                        failures.append(
                            f"{name} as {who}: {req.method} {template} needs {perm} "
                            f"but got {req.status}"
                        )
            if denied:
                denials += 1
                text = result.content[0].text if result.content else ""
                if not result.is_error or "role or scopes" not in text:
                    failures.append(f"{name} as {who}: denied without a clear error: {text!r}")
    assert not failures, "\n".join(failures)
    for name, case in CASES.items():
        if name in _PREVIEW_READ_404:
            continue
        # Every principal's call reached the tool's own route, so parity
        # was checked for each of them, not only the first reads.
        assert reached.get(name) == set(PARITY_PEOPLE), (name, case.route, reached.get(name))


@pytest.mark.anyio
async def test_step_up_actions_are_refused_with_a_pointer_to_the_web_app(app, tc, people):
    person = people["alice"]
    result, seen = await _call(
        app,
        tc,
        person.token,
        "update_subscription",
        {"subscription_id": person.ids["subscription"], "mode": "auto", "confirm": True},
    )
    assert result.is_error and "web app" in result.content[0].text
    assert seen == []  # refused before any request
    # The same route answers step_up_required for any token, and the client
    # turns it into the web-app message.
    api = ApiClient(BASE, token=person.token, transport=Recorder().transport(tc))
    with pytest.raises(Exception) as exc:
        await api.post("/api/connections/keys", {"provider": "fake", "fields": {"token": "x"}})
    assert getattr(exc.value, "code", None) == "step_up_required"
    assert "web app" in str(exc.value) and "second factor" in str(exc.value)


@pytest.mark.anyio
async def test_mcp_tools_never_show_another_users_rows(app, tc, people, seeded):
    """Alice's MCP server, given Bob's ids everywhere, shows none of Bob's rows."""
    alice, bob = people["alice"], people["bob"]
    bob_ids = bob.ids | {"tick": seeded["tick_id"]}
    markers = ["BOBBOOK", "BOBDRAFT", "BOBJOB", "BOB-secret", *bob_ids.values()]
    markers.remove(seeded["tick_id"])  # ticks are global
    for name, case in CASES.items():
        args = case.args(bob_ids)
        result, _ = await _call(app, tc, alice.token, name, args)
        text = " ".join(c.text for c in result.content if hasattr(c, "text"))
        sent = " ".join(str(v) for v in args.values())
        for marker in markers:
            if marker in sent:
                continue  # an error may echo the id Alice sent herself
            assert marker not in text, (name, marker)
        if any(v in sent for v in bob.ids.values()) and case.route[0] == "GET":
            assert result.is_error and "not found" in text.lower(), (name, text)
    # Bob's rows are untouched and still his.
    result, _ = await _call(app, tc, bob.token, "get_draft", {"draft_id": bob.ids["draft"]})
    assert not result.is_error
    for who in ("alice", "ada"):
        result, _ = await _call(
            app, tc, people[who].token, "get_insights", {"portfolio_id": bob.ids["portfolio"]}
        )
        assert result.is_error and "not found" in result.content[0].text.lower()
    # Tokens act as their owner: whoami names the right person.
    result, _ = await _call(app, tc, alice.token, "whoami", {})
    assert result.structured_content["user_id"] == alice.user_id
    assert re.fullmatch(r"usr_\w+", alice.user_id)
