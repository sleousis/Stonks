"""The connections migration, ``import-env`` for the single-owner Alpaca
install, threaded scheduled syncs and SnapTrade end to end (mock HTTP)."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from pydantic import SecretStr

from stonks.accounts import (
    DEFAULT_OWNER_ID,
    DEFAULT_PORTFOLIO_ID,
    AuditLog,
    PortfolioRepository,
    Role,
    Scope,
)
from stonks.connections.base import ProviderDisabled
from stonks.connections.service import ConnectionService
from stonks.connections.settings import ConnectionsConfig, SnapTradeConfig
from stonks.store.state import SqliteState

from .conftest import ADMIN

OWNER = Scope(user_id=DEFAULT_OWNER_ID, role=Role.ADMIN)
NOW = "2026-03-01T00:00:00+00:00"


def test_migration_adds_tables_and_keeps_old_snapshots_as_tick(tmp_path):
    s = SqliteState(tmp_path / "s.sqlite")
    s.migrate()
    for table in ("broker_connections", "broker_credentials", "broker_accounts",
                  "broker_positions", "broker_activities"):  # fmt: skip
        assert table in s.tables()
    s.execute(
        "INSERT INTO portfolio_snapshots (taken_at, cash, positions_json, total_value)"
        " VALUES (?, 1, '{}', 1)",
        [NOW],
    )
    assert s.sql("SELECT source FROM portfolio_snapshots")[0][0] == "tick"
    with pytest.raises(sqlite3.IntegrityError):
        s.execute(
            "INSERT INTO portfolio_snapshots (taken_at, cash, positions_json, total_value,"
            " source) VALUES (?, 1, '{}', 1, 'bogus')",
            [NOW],
        )
    s.close()


def test_connection_owner_and_provider_are_immutable(state, alice, bob, service):
    rec = service.connect_with_keys(alice, "fake", {"token": "tok-123456"})
    with pytest.raises(sqlite3.IntegrityError):
        state.execute(
            "UPDATE broker_connections SET user_id = ? WHERE id = ?", [bob.user_id, rec.id]
        )
    with pytest.raises(sqlite3.IntegrityError):
        state.execute("UPDATE broker_connections SET provider = 'x' WHERE id = ?", [rec.id])


# ---- import-env -------------------------------------------------------------------------


class FakeAlpaca:
    def get_account(self):
        return {"id": "alpaca-acct", "account_number": "PA0001", "cash": "500",
                "equity": "1500", "buying_power": "500", "currency": "USD", "status": "ACTIVE"}  # fmt: skip

    def get_all_positions(self):
        return [{"symbol": "AAPL", "asset_class": "us_equity", "qty": "5", "side": "long",
                 "current_price": "200", "market_value": "1000"}]  # fmt: skip

    def get(self, path, data=None):
        return []


def _settings(kind="alpaca", key="AKIMPORT123", secret="import-secret-xyz", paper=True):
    alpaca = SimpleNamespace(
        api_key=SecretStr(key) if key else None,
        secret_key=SecretStr(secret) if secret else None,
        paper=paper,
    )
    return SimpleNamespace(brokers=SimpleNamespace(kind=kind, alpaca=alpaca))


@pytest.fixture
def alpaca_service(state, box, clock):
    made = []

    def factory(api_key, secret_key, paper):
        made.append(paper)
        return FakeAlpaca()

    svc = ConnectionService(
        state,
        ConnectionsConfig(enabled_providers=("alpaca",)),
        box=box,
        clock=clock,
        transports={"alpaca": factory},
    )
    svc.made = made  # type: ignore[attr-defined]
    return svc


def _paper_subscription(state):
    state.execute(
        "INSERT INTO strategies (id, class_path, params_json, status, created_at, updated_at)"
        " VALUES ('s1', 'm:C', '{}', 'shadow', ?, ?)",
        [NOW, NOW],
    )
    state.execute(
        "INSERT INTO subscriptions (id, user_id, strategy_id, portfolio_id, mode, created_at,"
        " updated_at) VALUES ('sub_1', ?, 's1', ?, 'paper', ?, ?)",
        [DEFAULT_OWNER_ID, DEFAULT_PORTFOLIO_ID, NOW, NOW],
    )


def test_import_env_links_pf_default_and_switches_subscriptions(alpaca_service, state):
    _paper_subscription(state)
    result = alpaca_service.import_env(OWNER, _settings())
    assert result.status == "imported"
    assert result.subscriptions_switched == ("sub_1",)
    pf = PortfolioRepository(state).get(OWNER, DEFAULT_PORTFOLIO_ID)
    assert (pf.kind, pf.broker_connection_id, pf.external_account_id) == (
        "broker", result.connection_id, "alpaca-acct",
    )  # fmt: skip
    sub = state.sql("SELECT mode, auto_enabled_by FROM subscriptions WHERE id = 'sub_1'")[0]
    assert (sub["mode"], sub["auto_enabled_by"]) == ("auto", OWNER.actor)
    assert alpaca_service.made == [True]
    actions = [e.action for e in AuditLog(state).for_portfolio(OWNER, DEFAULT_PORTFOLIO_ID)]
    assert {"subscription.mode", "connection.import_env", "connection.link"} <= set(actions)
    dump = "\n".join(state.con.iterdump())
    assert "import-secret-xyz" not in dump and "AKIMPORT123" not in dump
    # Idempotent.
    again = alpaca_service.import_env(OWNER, _settings())
    assert again.status == "already_imported"
    assert state.sql("SELECT COUNT(*) FROM broker_connections")[0][0] == 1
    # And it syncs.
    synced = alpaca_service.sync(OWNER, result.connection_id)
    assert synced.status == "ok"
    assert synced.portfolios[0].portfolio_id == DEFAULT_PORTFOLIO_ID


def test_import_env_does_nothing_for_simulated_installs(alpaca_service):
    assert alpaca_service.import_env(OWNER, _settings(kind="simulated")).status == "not_applicable"


def test_import_env_needs_keys_and_the_provider_enabled(alpaca_service, state, box):
    with pytest.raises(Exception, match="ALPACA_API_KEY"):
        alpaca_service.import_env(OWNER, _settings(key=None))
    off = ConnectionService(state, ConnectionsConfig(), box=box)
    with pytest.raises(ProviderDisabled):
        off.import_env(OWNER, _settings())


# ---- threaded scheduled pass --------------------------------------------------------------


def test_sync_due_with_worker_threads(service, state, alice, bob):
    ids = [
        service.connect_with_keys(scope, "fake", {"token": f"tok-{i}-abcdef"}).id
        for i, scope in enumerate((alice, bob, alice))
    ]
    results = service.sync_due(ADMIN, max_workers=3)
    assert sorted(r.connection_id for r in results) == sorted(ids)
    assert all(r.status == "ok" for r in results)
    assert state.sql("SELECT COUNT(*) FROM portfolio_snapshots WHERE source = 'sync'")[0][0] == 3
    actors = {e.actor for e in AuditLog(state).by_actor(ADMIN)}
    assert actors == {ADMIN.actor}


# ---- SnapTrade end to end --------------------------------------------------------------------


class SnapServer:
    def __init__(self):
        self.users: dict[str, str] = {}

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        url = urlsplit(str(request.url))
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        route = f"{request.method} {url.path.removeprefix('/api/v1')}"
        if route == "POST /snapTrade/registerUser":
            import json

            uid = json.loads(request.content)["userId"]
            self.users[uid] = f"secret-for-{uid}"
            return httpx2.Response(200, json={"userId": uid, "userSecret": self.users[uid]})
        if route == "DELETE /snapTrade/deleteUser":
            self.users.pop(q["userId"], None)
            return httpx2.Response(200, json={})
        if self.users.get(q.get("userId", "")) != q.get("userSecret"):
            return httpx2.Response(401, json={"detail": "bad user secret"})
        if route == "POST /snapTrade/login":
            return httpx2.Response(200, json={"redirectURI": "https://app.snaptrade.com/p/1"})
        if route == "GET /accounts":
            return httpx2.Response(200, json=[{"id": "st-1", "name": "TFSA", "number": "998877",
                                               "balance": {"total": {"amount": 300, "currency": "CAD"}}}])  # fmt: skip
        if route == "GET /accounts/st-1/balances":
            return httpx2.Response(200, json=[{"currency": {"code": "CAD"}, "cash": 100}])
        if route == "GET /accounts/st-1/positions":
            return httpx2.Response(200, json=[
                {"symbol": {"symbol": {"symbol": "SHOP", "exchange": {"code": "TSX"},
                                       "currency": {"code": "CAD"}, "type": {"code": "cs"}}},
                 "units": 2, "price": 100},
            ])  # fmt: skip
        if route == "GET /activities":
            return httpx2.Response(200, json=[])
        return httpx2.Response(404, json={})


def test_snaptrade_portal_connect_sync_disconnect(state, box, clock, alice):
    server = SnapServer()
    svc = ConnectionService(
        state,
        ConnectionsConfig(
            enabled_providers=("snaptrade",),
            snaptrade=SnapTradeConfig(client_id="cid", consumer_key="ck-123456"),
        ),
        box=box,
        clock=clock,
        transports={"snaptrade": httpx2.MockTransport(server)},
    )
    link = svc.start_portal(alice, "snaptrade", "https://stonks.example/cb")
    assert link.url == "https://app.snaptrade.com/p/1"
    # The provider redirects to our callback with the state we embedded.
    token = state.sql("SELECT pending_state_hash FROM broker_connections")[0][0]
    assert token
    callback_state = _state_from_login(svc, alice, link.connection_id)
    rec = svc.complete_portal(alice, link.connection_id, callback_state, outcome="SUCCESS")
    assert rec.status == "active"
    result = svc.sync(alice, rec.id)
    assert result.status == "ok"
    snap = state.sql("SELECT * FROM portfolio_snapshots WHERE source = 'sync'")[0]
    assert snap["positions_json"] == '{"SHOP.TO": 2.0}'
    assert snap["total_value"] == 300
    dump = "\n".join(state.con.iterdump())
    assert f"secret-for-stonks-{rec.id}" not in dump
    out = svc.disconnect(alice, rec.id)
    assert out.remote_removed is True
    assert server.users == {}


def _state_from_login(svc, scope, connection_id):
    """Start a fresh link for the same connection, capturing the callback
    URL sent to SnapTrade's login endpoint."""
    captured = {}
    original = svc._transports["snaptrade"]

    def spy(request):
        if request.url.path.endswith("/snapTrade/login"):
            import json

            captured["redirect"] = json.loads(request.content)["customRedirect"]
        return original.handle_request(request)

    svc._transports["snaptrade"] = httpx2.MockTransport(spy)
    try:
        svc.start_portal(
            scope, "snaptrade", "https://stonks.example/cb", connection_id=connection_id
        )
    finally:
        svc._transports["snaptrade"] = original
    query = parse_qs(urlsplit(captured["redirect"]).query)
    assert query["connection_id"] == [connection_id]
    return query["state"][0]
