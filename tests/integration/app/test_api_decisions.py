"""Why did or didn't we trade (roadmap 23.7): each caller reads their own
book's decision rows, another user's portfolio is a 404, and the MCP tool
reads the same."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from stonks.accounts import PortfolioRepository, Role, Scope
from stonks.api import create_app
from stonks.app.context import AppContext
from stonks.app.services import Services
from stonks.mcp.server import build_server
from stonks.portfolio.explain import TickerDecision
from stonks.production.decisions import record_decisions
from stonks.security import KeyRing, SecretBox, generate_key
from stonks.store.state import SqliteState
from tests.integration.app.test_api import REMOTE
from tests.integration.app.test_mcp_server import _api, call
from tests.integration.auth.helpers import add_user, make_service, session_principal

BASE = "https://testserver"


def _rows(state: SqliteState, pf: str) -> None:
    record_decisions(
        state,
        tick_id="t1",
        portfolio_id=pf,
        as_of=date(2026, 3, 20),
        decisions=[
            TickerDecision(
                ticker="UP.US",
                step="risk_rule",
                outcome="trimmed",
                strategy_id="mom",
                strategies=("mom",),
                detail={"rule": "max_weight_per_ticker"},
            ),
            TickerDecision(ticker="DN.US", step="rank", outcome="kept_out", strategies=("rev",)),
        ],
    )


@pytest.fixture
def auth(settings, seeded):
    box = SecretBox(KeyRing.parse(f"k1:{generate_key()}"))
    return make_service(
        settings.state.path, legacy="test-token-123", box=box, clock=lambda: datetime.now(UTC)
    )


@pytest.fixture
def app(settings, seeded, fake_source, auth):
    settings.api.allowed_hosts = ["testserver"]
    svc = Services.create(AppContext(settings, source_factory=lambda: fake_source))
    application = create_app(settings, services=svc)
    application.state.auth = auth
    return application


@pytest.fixture
def people(settings, auth):
    path = settings.state.path
    alice = add_user(path, "alice@example.com")
    bob = add_user(path, "bob@example.com")
    with SqliteState(path) as state:
        repo = PortfolioRepository(state)
        pf_a = repo.create(Scope(user_id=alice, role=Role.TRADER), name="Alice").id
        pf_b = repo.create(Scope(user_id=bob, role=Role.TRADER), name="Bob").id
        _rows(state, pf_a)
        _rows(state, pf_b)
    tokens = {}
    for name, uid in (("alice", alice), ("bob", bob)):
        _, token = auth.create_token(session_principal(uid, Role.TRADER), name="t", scopes=["read"])
        tokens[name] = {"Authorization": f"Bearer {token}"}
    return {"pf_a": pf_a, "pf_b": pf_b, **tokens}


def test_why_not_reads_your_own_book(app, people):
    with TestClient(app, client=REMOTE, base_url=BASE) as c:
        page = c.get("/api/decisions", params={"ticker": "up.us"}, headers=people["alice"])
        by_strategy = c.get(
            "/api/decisions", params={"strategy_id": "rev"}, headers=people["alice"]
        ).json()
    assert page.status_code == 200
    body = page.json()
    assert body["total"] == 1
    [row] = body["items"]
    assert row["portfolio_id"] == people["pf_a"]
    assert (row["step"], row["outcome"]) == ("risk_rule", "trimmed")
    assert row["detail"]["rule"] == "max_weight_per_ticker"
    assert "max_weight_per_ticker" in row["summary"]
    assert [r["ticker"] for r in by_strategy["items"]] == ["DN.US"]


def test_another_users_portfolio_is_a_404(app, people):
    with TestClient(app, client=REMOTE, base_url=BASE) as c:
        resp = c.get(
            "/api/decisions", params={"portfolio_id": people["pf_b"]}, headers=people["alice"]
        )
    assert resp.status_code == 404


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def loopback_client(settings, seeded, fake_source):
    settings.api.allowed_hosts = ["testserver"]
    with SqliteState(settings.state.path) as state:
        _rows(state, "pf_default")
    app = create_app(settings, source_factory=lambda: fake_source)
    with TestClient(app, client=("127.0.0.1", 50000)) as tc:
        yield tc


@pytest.mark.anyio
async def test_mcp_tool_reads_the_rows(loopback_client):
    async with Client(build_server(_api(loopback_client), max_wait_seconds=60)) as mcp:
        page = await call(mcp, "list_trade_decisions", {"ticker": "DN.US"})
        assert page["total"] == 1 and page["items"][0]["step"] == "rank"
