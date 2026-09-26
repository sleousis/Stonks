"""TO-03: reading the health report never changes trading. Only the
scheduled health job (``POST /api/health/run``, admins and the scheduler
token) and ``stonks health`` open or clear the global operational halt."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from stonks.accounts import DEFAULT_OWNER_ID, Role
from stonks.api import create_app
from stonks.app.context import AppContext
from stonks.app.services import Services
from stonks.auth import AuthService
from stonks.production.halts import list_halts, trip_halt
from stonks.scheduling.in_process import InProcessExecutor
from stonks.security import KeyRing, SecretBox, generate_key
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState
from tests.integration.app.test_api import REMOTE
from tests.integration.app.test_scheduling_backends import _ctx
from tests.integration.auth.helpers import add_user, make_service, session_principal

BASE = "https://testserver"


@pytest.fixture
def fresh_bar(settings, seeded):
    """A bar for today, so ``FRESH.US`` passes the freshness check."""
    today = datetime.now(UTC).date()
    with DuckDBLake(settings.lake.path) as lake:
        lake.upsert_prices(
            pd.DataFrame(
                [
                    {
                        "ticker": "FRESH.US",
                        "date": today,
                        "open": 10.0,
                        "high": 10.0,
                        "low": 10.0,
                        "close": 10.0,
                        "adj_close": 10.0,
                        "volume": 1000,
                    }
                ]
            )
        )


@pytest.fixture
def auth(settings, seeded) -> AuthService:
    box = SecretBox(KeyRing.parse(f"k1:{generate_key()}"))
    return make_service(
        settings.state.path, legacy="test-token-123", box=box, clock=lambda: datetime.now(UTC)
    )


@pytest.fixture
def client(settings, seeded, fresh_bar, fake_source, auth):
    settings.api.allowed_hosts = ["testserver"]
    svc = Services.create(AppContext(settings, source_factory=lambda: fake_source))
    app = create_app(settings, services=svc)
    app.state.auth = auth
    with TestClient(app, client=REMOTE, base_url=BASE) as c:
        yield c


def _headers(auth, user_id: str, role: Role, scopes: list[str]) -> dict:
    _, token = auth.create_token(session_principal(user_id, role), name="t", scopes=scopes)
    return {"Authorization": f"Bearer {token}"}


def _viewer(settings, auth) -> dict:
    uid = add_user(settings.state.path, "viewer@example.com", Role.VIEWER)
    return _headers(auth, uid, Role.VIEWER, ["read"])


def _admin(auth) -> dict:
    return _headers(auth, DEFAULT_OWNER_ID, Role.ADMIN, ["read", "admin"])


def _open_halts(settings) -> list:
    with SqliteState(settings.state.path) as state:
        return list_halts(state)


def test_a_viewer_reading_a_failing_report_opens_no_halt(client, settings, auth):
    r = client.get(
        "/api/health/report", params={"tickers": ["NOPE.US"]}, headers=_viewer(settings, auth)
    )
    assert r.status_code == 200
    assert r.json()["healthy"] is False
    assert _open_halts(settings) == []


def test_reading_a_passing_report_does_not_clear_the_operational_halt(client, settings, auth):
    with SqliteState(settings.state.path) as state:
        trip_halt(
            state,
            "operational",
            reason="health: freshness:UP.US",
            actor="service:health",
            scope="global",
        )
    r = client.get(
        "/api/health/report", params={"tickers": ["FRESH.US"]}, headers=_viewer(settings, auth)
    )
    assert r.status_code == 200
    [halt] = _open_halts(settings)
    assert halt.kind == "operational"
    # the report still lists the open halt
    checks = {c["name"]: c for c in r.json()["checks"]}
    assert checks["risk_halts"]["ok"] is False


def test_the_health_run_route_needs_operations_run(client, settings, auth):
    r = client.post(
        "/api/health/run", json={"tickers": ["NOPE.US"]}, headers=_viewer(settings, auth)
    )
    assert r.status_code == 403
    assert _open_halts(settings) == []


def test_the_health_run_route_opens_and_clears_the_halt(client, settings, auth):
    r = client.post("/api/health/run", json={"tickers": ["NOPE.US"]}, headers=_admin(auth))
    assert r.status_code == 200, r.text
    [halt] = _open_halts(settings)
    assert halt.kind == "operational"
    r = client.post("/api/health/run", json={"tickers": ["FRESH.US"]}, headers=_admin(auth))
    assert r.status_code == 200
    assert _open_halts(settings) == []


def test_the_in_process_health_job_syncs_the_halt(settings, services):
    ex = InProcessExecutor(services)
    ctx, _ = _ctx(settings, ex, "health", date(2026, 9, 25), tickers=["NOPE.US"])
    assert ex.execute(ctx).status == "failed"
    [halt] = _open_halts(settings)
    assert halt.kind == "operational"
