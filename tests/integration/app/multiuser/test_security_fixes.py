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
from stonks.store.state import SqliteState
from tests.integration.app.multiuser.conftest import BASE, bearer
from tests.integration.app.test_api import REMOTE


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


# ---- AS-06 job and draft owners --------------------------------------------------

from tests.integration.app.test_studio import TREND as TREND_SPEC  # noqa: E402


def test_as06_a_job_records_its_owner_and_only_the_owner_or_an_admin_sees_it(client, app, people):
    from tests.integration.app.test_api import _backtest_body

    alice, bob, ada = (people[n]["headers"] for n in ("alice", "bob", "ada"))
    resp = client.post("/api/lab/backtests", json=_backtest_body(), headers=alice)
    assert resp.status_code == 202, resp.text
    job = resp.json()
    assert job["owner_id"] == people["alice"]["id"]

    assert client.get(f"/api/jobs/{job['id']}", headers=alice).status_code == 200
    assert client.get(f"/api/jobs/{job['id']}", headers=bob).status_code == 404
    assert client.get(f"/api/jobs/{job['id']}", headers=ada).status_code == 200
    assert job["id"] not in client.get("/api/jobs", headers=bob).text
    assert job["id"] in client.get("/api/jobs", headers=ada).text
    token = client.post(f"/api/jobs/{job['id']}/stream-token", headers=bob)
    assert token.status_code == 404
    result = client.get(f"/api/lab/backtests/{job['id']}/result", headers=bob)
    assert result.status_code == 404


def test_as06_another_trader_cannot_cancel_a_job(client, app, people):
    store = app.state.services.runner.store
    queued = store.create("backtest", {}, owner_id=people["alice"]["id"])
    bob, ada = people["bob"]["headers"], people["ada"]["headers"]
    assert client.post(f"/api/jobs/{queued.id}/cancel", headers=bob).status_code == 404
    assert store.get(queued.id).status == "queued"
    assert client.post(f"/api/jobs/{queued.id}/cancel", headers=ada).status_code == 200


def test_as06_drafts_record_an_owner_and_other_traders_get_404(client, people):
    alice, bob, ada = (people[n]["headers"] for n in ("alice", "bob", "ada"))
    created = client.post(
        "/api/studio/drafts", json={"name": "mine", "spec": TREND_SPEC}, headers=alice
    )
    assert created.status_code == 201, created.text
    draft = created.json()
    assert draft["owner_id"] == people["alice"]["id"]
    url = f"/api/studio/drafts/{draft['id']}"
    assert client.get(url, headers=bob).status_code == 404
    assert client.patch(url, json={"name": "x"}, headers=bob).status_code == 404
    assert client.post(f"{url}/validate", json={}, headers=bob).status_code == 404
    assert client.delete(url, headers=bob).status_code == 404
    assert draft["id"] not in client.get("/api/studio/drafts", headers=bob).text
    assert draft["id"] in client.get("/api/studio/drafts", headers=ada).text
    assert client.patch(url, json={"name": "y"}, headers=ada).status_code == 200
    assert client.delete(url, headers=ada).status_code == 200


# ---- AS-07 universe writes on the lake_write lane -----------------------------------


def test_as07_universe_writes_run_on_the_lake_write_lane(client, app, monkeypatch):
    from tests.integration.app.test_api import AUTH

    runner = app.state.services.runner
    lanes: list[str] = []
    real = runner.run_in_lane

    def spy(lane, fn, **kwargs):
        lanes.append(lane)
        return real(lane, fn, **kwargs)

    monkeypatch.setattr(runner, "run_in_lane", spy)
    body = {"id": "mine", "kind": "list", "spec": {"tickers": ["UP.US"]}}
    assert client.post("/api/universes", json=body, headers=AUTH).status_code == 201
    content = "date,ticker,action\n2026-01-02,UP.US,member\n"
    imported = client.post(
        "/api/universes/index-history",
        json={"index_id": "toy", "format": "csv", "content": content},
        headers=AUTH,
    )
    assert imported.status_code == 200, imported.text
    assert client.delete("/api/universes/mine", headers=AUTH).status_code == 200
    assert lanes == ["lake_write"] * 3


def test_as07_a_universe_cannot_be_deleted_while_its_refresh_is_pending(client, app):
    from stonks.app.universes import UNIVERSE_REFRESH_JOB
    from tests.integration.app.test_api import AUTH

    body = {"id": "busy", "kind": "list", "spec": {"tickers": ["UP.US"]}}
    assert client.post("/api/universes", json=body, headers=AUTH).status_code == 201
    store = app.state.services.runner.store
    queued = store.create(UNIVERSE_REFRESH_JOB, {"universe_id": "busy"})
    resp = client.delete("/api/universes/busy", headers=AUTH)
    assert resp.status_code == 409 and queued.id in resp.json()["detail"]
    store.cancel(queued.id)
    assert client.delete("/api/universes/busy", headers=AUTH).status_code == 200


# ---- AS-09 / AS-19 read-permission POSTs for viewers --------------------------------


def test_as09_a_viewer_gets_a_stream_token_for_their_own_job_but_cannot_write(client, app, people):
    from tests.integration.app.test_api import _backtest_body

    vic = people["vic"]
    job = app.state.services.runner.store.create("backtest", {}, owner_id=vic["id"])
    token = client.post(f"/api/jobs/{job.id}/stream-token", headers=vic["headers"])
    assert token.status_code == 200, token.text
    denied = client.post("/api/lab/backtests", json=_backtest_body(), headers=vic["headers"])
    assert denied.status_code == 403


def test_as19_a_viewer_can_mark_their_feed_read(client, settings, people):
    vic = people["vic"]
    _insert_alert(settings.state.path, "for-vic", vic["id"])
    assert client.get("/api/notifications", headers=vic["headers"]).json()["unread_count"] == 1
    resp = client.post("/api/notifications/read", json={}, headers=vic["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["unread_count"] == 0


# ---- AS-13 cancel permission per job kind ------------------------------------------


def test_as13_a_trader_cancels_their_own_queued_universe_job(client, app, people):
    from stonks.app.universes import UNIVERSE_ENSURE_JOB, UNIVERSE_REFRESH_JOB

    store = app.state.services.runner.store
    alice = people["alice"]
    for kind in (UNIVERSE_REFRESH_JOB, UNIVERSE_ENSURE_JOB):
        job = store.create(kind, {"universe_id": "u"}, owner_id=alice["id"])
        resp = client.post(f"/api/jobs/{job.id}/cancel", headers=alice["headers"])
        assert resp.status_code == 200, (kind, resp.text)
    # Operator jobs stay admin-only.
    ingest = store.create("ingest", {}, owner_id=alice["id"])
    assert client.post(f"/api/jobs/{ingest.id}/cancel", headers=alice["headers"]).status_code == 403
