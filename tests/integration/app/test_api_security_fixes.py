"""Fix waves for the service-surface review (roadmap 18.2): each test names
the finding it pins (AS-xx)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from stonks.accounts import Role
from stonks.api import create_app
from stonks.app.context import AppContext
from stonks.app.services import Services
from stonks.auth import AuthService
from stonks.security import KeyRing, SecretBox, generate_key
from stonks.store.state import SqliteState
from tests.integration.app.test_api import REMOTE
from tests.integration.auth.helpers import add_user, make_service, session_principal

BASE = "https://testserver"


@pytest.fixture
def box() -> SecretBox:
    return SecretBox(KeyRing.parse(f"k1:{generate_key()}"))


@pytest.fixture
def auth(settings, seeded, box) -> AuthService:
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
def client(app):
    with TestClient(app, client=REMOTE, base_url=BASE) as c:
        yield c


def bearer(auth: AuthService, user_id: str, role: Role, scopes: list[str]) -> dict[str, str]:
    _, token = auth.create_token(session_principal(user_id, role), name="t", scopes=scopes)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def people(settings, auth) -> dict[str, dict]:
    """Alice (trader), Bob (trader), Vic (viewer) and Ada (admin), with full tokens."""
    path = settings.state.path
    out: dict[str, dict] = {}
    for name, role, scopes in [
        ("alice", Role.TRADER, ["read", "trade", "lab"]),
        ("bob", Role.TRADER, ["read", "trade", "lab"]),
        ("vic", Role.VIEWER, ["read"]),
        ("ada", Role.ADMIN, ["read", "trade", "lab", "admin"]),
    ]:
        uid = add_user(path, f"{name}@example.com", role)
        out[name] = {"id": uid, "headers": bearer(auth, uid, role, scopes), "role": role}
    return out


def _insert_alert(path, title: str, user_id: str | None) -> None:
    with SqliteState(path) as state:
        state.execute(
            "INSERT INTO alerts (level, title, message, created_at, user_id, category)"
            " VALUES ('info', ?, 'm', '2026-09-26T00:00:00+00:00', ?, 'system')",
            [title, user_id],
        )


# ---- AS-01 alerts ------------------------------------------------------------------


def test_as01_alerts_show_only_the_callers_notifications(client, settings, people):
    _insert_alert(settings.state.path, "bob-only-alert", people["bob"]["id"])
    _insert_alert(settings.state.path, "alice-alert", people["alice"]["id"])
    _insert_alert(settings.state.path, "admins-audience", None)

    vic = client.get("/api/alerts", headers=people["vic"]["headers"])
    assert vic.status_code == 200
    assert vic.json()["total"] == 0 and "bob-only-alert" not in vic.text

    alice = client.get("/api/alerts", headers=people["alice"]["headers"]).json()
    assert [a["title"] for a in alice["items"]] == ["alice-alert"]

    # Admins also see the admin audience (user_id NULL), never another user's rows.
    ada = client.get("/api/alerts", headers=people["ada"]["headers"]).json()
    assert [a["title"] for a in ada["items"]] == ["admins-audience"]


def test_as01_alerts_need_a_credential_even_on_loopback(settings, seeded, fake_source):
    settings.api.allowed_hosts = ["testserver"]
    app = create_app(settings, source_factory=lambda: fake_source)
    with TestClient(app, client=("127.0.0.1", 5000)) as c:
        assert c.get("/api/alerts").status_code == 401


# ---- AS-03 Alpaca status ---------------------------------------------------------


class _CountingAlpaca:
    calls = 0

    def fetch_account(self):
        from stonks.execution.brokers.base import BrokerAccount

        type(self).calls += 1
        return BrokerAccount(
            cash=1.0, equity=2.0, buying_power=3.0, currency="USD", status="ACTIVE"
        )

    def get_market_clock(self):
        from stonks.execution.brokers.base import MarketClock

        now = datetime(2026, 3, 20, 15, tzinfo=UTC)
        return MarketClock(timestamp=now, is_open=True, next_open=now, next_close=now)


@pytest.fixture
def alpaca_client(settings, seeded, fake_source, auth):
    from pydantic import SecretStr

    settings.api.allowed_hosts = ["testserver"]
    settings.brokers.kind = "alpaca"
    settings.brokers.alpaca.api_key = SecretStr("k-123")
    settings.brokers.alpaca.secret_key = SecretStr("s-456")
    _CountingAlpaca.calls = 0
    svc = Services.create(
        AppContext(settings, source_factory=lambda: fake_source),
        broker_connector=lambda s: _CountingAlpaca(),
    )
    application = create_app(settings, services=svc)
    application.state.auth = auth
    with TestClient(application, client=REMOTE, base_url=BASE) as c:
        yield c


def test_as03_alpaca_status_is_only_for_the_account_owner(alpaca_client, people):
    from tests.integration.app.test_api import AUTH

    for name in ("vic", "alice", "ada"):
        resp = alpaca_client.get("/api/brokers/alpaca/status", headers=people[name]["headers"])
        assert resp.status_code == 404, (name, resp.text)
        assert "equity" not in resp.text
    # The legacy token is the bootstrap admin, who owns pf_default (the Alpaca book).
    owner = alpaca_client.get("/api/brokers/alpaca/status", headers=AUTH)
    assert owner.status_code == 200 and owner.json()["account"]["equity"] == 2.0


def test_as03_alpaca_status_is_cached_between_calls(alpaca_client):
    from tests.integration.app.test_api import AUTH

    for _ in range(3):
        assert alpaca_client.get("/api/brokers/alpaca/status", headers=AUTH).status_code == 200
    assert _CountingAlpaca.calls == 1


# ---- AS-04 / AS-10 halts ---------------------------------------------------------


def _global_breaker(path) -> int:
    from datetime import date

    from stonks.production.halts import trip_halt

    with SqliteState(path) as state:
        halt, _ = trip_halt(
            state,
            "operational",
            reason="dd",
            actor="service:tick",
            scope="global",
            on=date.today(),
        )
    return halt.id


def test_as04_global_halt_actions_need_the_admin_scope_not_just_the_role(
    client, settings, auth, people
):
    ada = people["ada"]["id"]
    trade_only = bearer(auth, ada, Role.ADMIN, ["read", "trade"])
    kill = {"scope": "global", "reason": "r"}
    denied = client.post("/api/halts/kill", json=kill, headers=trade_only)
    assert denied.status_code == 403 and denied.json()["detail"].startswith("forbidden")
    halt_id = _global_breaker(settings.state.path)
    clear = client.post(f"/api/halts/{halt_id}/clear", json={"reason": "r"}, headers=trade_only)
    assert clear.status_code == 403

    full = people["ada"]["headers"]
    assert client.post("/api/halts/kill", json=kill, headers=full).status_code == 201
    ok = client.post(f"/api/halts/{halt_id}/clear", json={"reason": "r"}, headers=full)
    assert ok.status_code == 200, ok.text


def test_as10_a_trader_refused_a_global_action_gets_403_not_422(client, settings, people):
    alice = people["alice"]["headers"]
    kill = client.post("/api/halts/kill", json={"scope": "global", "reason": "r"}, headers=alice)
    assert kill.status_code == 403 and kill.json()["detail"].startswith("forbidden")
    halt_id = _global_breaker(settings.state.path)
    clear = client.post(f"/api/halts/{halt_id}/clear", json={"reason": "r"}, headers=alice)
    assert clear.status_code == 403
