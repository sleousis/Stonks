"""Live risk routes (BL-47): each caller reads their own book's snapshots,
another user's portfolio is a 404, and the MCP tools read the same."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from stonks.accounts import PortfolioRepository, Role, Scope
from stonks.api import create_app
from stonks.app.context import AppContext
from stonks.app.services import Services
from stonks.mcp.server import build_server
from stonks.production.risk_metrics import RiskSnapshot, write_snapshot
from stonks.security import KeyRing, SecretBox, generate_key
from stonks.store.state import SqliteState
from tests.integration.app.test_api import REMOTE
from tests.integration.app.test_mcp_server import _api, call
from tests.integration.auth.helpers import add_user, make_service, session_principal

BASE = "https://testserver"


def _snap(pf: str, day: str, strategy_id: str = "", ratio: float | None = 1.0) -> RiskSnapshot:
    from datetime import date

    return RiskSnapshot(
        portfolio_id=pf,
        strategy_id=strategy_id,
        as_of=date.fromisoformat(day),
        tick_id=None,
        value=1000.0,
        exposures={"UP.US": 600.0},
        sigma=0.01,
        var_95=0.0164,
        var_99=0.0233,
        es_95=0.0206,
        es_99=0.0267,
        observations=250,
        realized_return=-0.002,
        pnl=-2.0,
        violation_95=False,
        violation_99=False,
        window_days=100,
        violations_95=5,
        violations_99=1,
        violation_ratio_95=ratio,
        violation_ratio_99=1.0,
        kupiec_p_95=0.9,
        kupiec_p_99=0.9,
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
        for day in ("2026-03-19", "2026-03-20"):
            write_snapshot(state, _snap(pf_a, day))
        write_snapshot(state, _snap(pf_a, "2026-03-20", "mom", ratio=2.4))
        write_snapshot(state, _snap(pf_b, "2026-03-20"))
    tokens = {}
    for name, uid in (("alice", alice), ("bob", bob)):
        _, token = auth.create_token(
            session_principal(uid, Role.TRADER), name="t", scopes=["read", "trade"]
        )
        tokens[name] = {"Authorization": f"Bearer {token}"}
    return {"pf_a": pf_a, "pf_b": pf_b, **tokens}


def test_live_risk_reads_your_own_latest_day(app, people):
    with TestClient(app, client=REMOTE, base_url=BASE) as c:
        resp = c.get("/api/risk/live", headers=people["alice"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["portfolio_id"] == people["pf_a"] and body["as_of"] == "2026-03-20"
    assert body["portfolio"]["strategy_id"] is None
    assert body["portfolio"]["var_95"] == pytest.approx(0.0164)
    [sleeve] = body["strategies"]
    assert sleeve["strategy_id"] == "mom" and sleeve["ratio_out_of_band"] is True


def test_snapshots_page_the_whole_book_or_one_sleeve(app, people):
    with TestClient(app, client=REMOTE, base_url=BASE) as c:
        whole = c.get("/api/risk/snapshots", headers=people["alice"]).json()
        sleeve = c.get(
            "/api/risk/snapshots", params={"strategy_id": "mom"}, headers=people["alice"]
        ).json()
    assert whole["total"] == 2
    assert [i["as_of"] for i in whole["items"]] == ["2026-03-20", "2026-03-19"]
    assert sleeve["total"] == 1 and sleeve["items"][0]["strategy_id"] == "mom"


def test_another_users_portfolio_is_a_404(app, people):
    with TestClient(app, client=REMOTE, base_url=BASE) as c:
        for path in ("/api/risk/live", "/api/risk/snapshots"):
            resp = c.get(path, params={"portfolio_id": people["pf_b"]}, headers=people["alice"])
            assert resp.status_code == 404, path


def test_the_admins_default_book_is_a_404_for_a_trader(app, people):
    with TestClient(app, client=REMOTE, base_url=BASE) as c:
        body = c.get(
            "/api/risk/live", params={"portfolio_id": "pf_default"}, headers=people["alice"]
        )
    # pf_default belongs to the bootstrap admin, not alice
    assert body.status_code == 404


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def loopback_client(settings, seeded, fake_source):
    settings.api.allowed_hosts = ["testserver"]
    with SqliteState(settings.state.path) as state:
        write_snapshot(state, _snap("pf_default", "2026-03-20"))
    app = create_app(settings, source_factory=lambda: fake_source)
    with TestClient(app, client=("127.0.0.1", 50000)) as tc:
        yield tc


@pytest.mark.anyio
async def test_mcp_tools_read_the_snapshots(loopback_client):
    async with Client(build_server(_api(loopback_client), max_wait_seconds=60)) as mcp:
        live = await call(mcp, "live_risk", {})
        assert live["as_of"] == "2026-03-20" and live["portfolio"]["value"] == 1000.0
        page = await call(mcp, "risk_snapshots", {"limit": 5})
        assert page["total"] == 1
