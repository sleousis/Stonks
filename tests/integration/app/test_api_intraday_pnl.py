"""Intraday snapshot routes (roadmap 21.3.3): each caller pages their own
book's rows for a day, another user's portfolio is a 404, and the MCP tool
reads the same."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from stonks.accounts import PortfolioRepository, Role, Scope
from stonks.api import create_app
from stonks.app.context import AppContext
from stonks.app.intraday_pnl import IntradayPnlService
from stonks.app.services import Services
from stonks.mcp.server import build_server
from stonks.production.intraday_pnl import IntradaySnapshot, write_intraday_snapshot
from stonks.security import KeyRing, SecretBox, generate_key
from stonks.store.state import SqliteState
from tests.integration.app.test_api import REMOTE
from tests.integration.app.test_mcp_server import _api, call
from tests.integration.auth.helpers import add_user, make_service, session_principal

BASE = "https://testserver"
OPEN = datetime(2026, 9, 28, 13, 30, tzinfo=UTC)


def _snap(pf: str, minute: int, strategy_id: str = "", day: date | None = None) -> IntradaySnapshot:
    at = OPEN + timedelta(minutes=minute)
    if day is not None:
        at = datetime.combine(day, at.time(), tzinfo=UTC)
    return IntradaySnapshot(
        portfolio_id=pf,
        strategy_id=strategy_id,
        day=at.date(),
        at=at,
        start_value=2000.0,
        value=2000.0 + minute,
        realised=0.0,
        unrealised=float(minute),
        fees=0.0,
        pnl=float(minute),
        day_return=minute / 2000.0,
        high_water_pnl=float(minute),
        drawdown=0.0,
        gross_exposure=1000.0 + minute,
        net_exposure=1000.0 + minute,
        exposures={"A.US": 1000.0 + minute},
        fills=0,
        unmarked=0,
        stale_marks=0,
        max_mark_age_seconds=30.0,
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
        for minute in (5, 10, 15):
            write_intraday_snapshot(state, _snap(pf_a, minute))
        write_intraday_snapshot(state, _snap(pf_a, 15, "mom"))
        write_intraday_snapshot(state, _snap(pf_a, 5, day=date(2026, 9, 25)))
        write_intraday_snapshot(state, _snap(pf_b, 5))
    tokens = {}
    for name, uid in (("alice", alice), ("bob", bob)):
        _, token = auth.create_token(
            session_principal(uid, Role.TRADER), name="t", scopes=["read", "trade"]
        )
        tokens[name] = {"Authorization": f"Bearer {token}"}
    return {"pf_a": pf_a, "pf_b": pf_b, **tokens}


def test_the_latest_day_pages_newest_first(app, people):
    with TestClient(app, client=REMOTE, base_url=BASE) as c:
        resp = c.get(
            "/api/risk/intraday",
            params={"portfolio_id": people["pf_a"], "limit": 2},
            headers=people["alice"],
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3 and body["limit"] == 2
    assert [i["at"][11:16] for i in body["items"]] == ["13:45", "13:40"]
    first = body["items"][0]
    assert first["strategy_id"] is None and first["day"] == "2026-09-28"
    assert first["pnl"] == 15.0 and first["exposures"] == {"A.US": 1015.0}


def test_one_day_one_sleeve_or_every_book(app, people):
    with TestClient(app, client=REMOTE, base_url=BASE) as c:
        old = c.get(
            "/api/risk/intraday",
            params={"portfolio_id": people["pf_a"], "day": "2026-09-25"},
            headers=people["alice"],
        ).json()
        sleeve = c.get(
            "/api/risk/intraday",
            params={"portfolio_id": people["pf_a"], "strategy_id": "mom"},
            headers=people["alice"],
        ).json()
        every = c.get(
            "/api/risk/intraday",
            params={"portfolio_id": people["pf_a"], "all_books": True},
            headers=people["alice"],
        ).json()
    assert old["total"] == 1 and old["items"][0]["day"] == "2026-09-25"
    assert sleeve["total"] == 1 and sleeve["items"][0]["strategy_id"] == "mom"
    assert every["total"] == 4


def test_another_users_portfolio_is_a_404(app, people):
    with TestClient(app, client=REMOTE, base_url=BASE) as c:
        resp = c.get(
            "/api/risk/intraday", params={"portfolio_id": people["pf_b"]}, headers=people["alice"]
        )
    assert resp.status_code == 404


def test_the_route_declares_the_read_permission(app):
    from stonks.api.deps import route_permissions
    from stonks.auth import Permission

    perms = {(m, p): perm for m, p, perm in route_permissions(app.routes)}
    assert perms[("GET", "/api/risk/intraday")] == Permission.READ


def test_a_portfolio_without_rows_is_an_empty_page(settings, seeded):
    page = IntradayPnlService(AppContext(settings)).list("pf_default")
    assert page.total == 0 and page.items == []


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def loopback_client(settings, seeded, fake_source):
    settings.api.allowed_hosts = ["testserver"]
    with SqliteState(settings.state.path) as state:
        write_intraday_snapshot(state, _snap("pf_default", 5))
        write_intraday_snapshot(state, _snap("pf_default", 10))
    app = create_app(settings, source_factory=lambda: fake_source)
    with TestClient(app, client=("127.0.0.1", 50000)) as tc:
        yield tc


@pytest.mark.anyio
async def test_mcp_tool_reads_the_intraday_snapshots(loopback_client):
    async with Client(build_server(_api(loopback_client), max_wait_seconds=60)) as mcp:
        page = await call(mcp, "list_intraday_snapshots", {"limit": 1})
    assert page["total"] == 2 and page["items"][0]["pnl"] == 10.0
