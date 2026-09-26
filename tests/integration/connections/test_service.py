"""Connections service over a migrated state DB with the fake providers:
connect (API key and hosted portal), link, sync, disconnect, tenant
isolation, secrets hygiene and idempotent re-syncs."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from stonks.accounts import AuditLog, NotFound, PortfolioRepository
from stonks.connections.base import (
    Activity,
    ConnectionsError,
    ProviderAuthError,
    ProviderDisabled,
    ProviderUnavailable,
    RateLimited,
)
from stonks.connections.providers import fake
from stonks.connections.providers.fake import book_for, fake_position
from stonks.connections.service import ConnectionService
from stonks.connections.settings import ConnectionsConfig
from stonks.security import KeyRing, SecretBox, generate_key

from .conftest import ADMIN

TOKEN = "demo-token-123456"


def _connect(service, scope, token=TOKEN):
    return service.connect_with_keys(scope, "fake", {"token": token}, label="Demo")


def _portfolio_for(service, scope, connection_id):
    [acc] = service.accounts(scope, connection_id)
    return acc.portfolio_id


# ---- providers -------------------------------------------------------------------


def test_only_enabled_providers_are_offered(service, alice, state, box):
    names = [p.name for p in service.available_providers(alice)]
    assert names == ["fake", "fake_portal"]
    nothing = ConnectionService(state, ConnectionsConfig(), box=box)
    assert nothing.available_providers(alice) == []
    with pytest.raises(ProviderDisabled):
        nothing.connect_with_keys(alice, "fake", {"token": TOKEN})
    with pytest.raises(ProviderDisabled):
        service.connect_with_keys(alice, "alpaca", {"api_key": "a", "secret_key": "b"})


# ---- connect with API keys ----------------------------------------------------


def test_connect_with_keys_seals_credentials_and_links_a_broker_portfolio(service, alice, state):
    rec = _connect(service, alice)
    assert rec.status == "active"
    assert rec.user_id == alice.user_id
    row = state.sql("SELECT * FROM broker_credentials WHERE connection_id = ?", [rec.id])[0]
    assert TOKEN not in row["ciphertext"]
    assert row["key_id"] == "k1"
    [acc] = service.accounts(alice, rec.id)
    assert (acc.external_account_id, acc.number_mask) == ("fake-acc-1", "…0001")
    pf = PortfolioRepository(state).get(alice, acc.portfolio_id)
    assert (pf.kind, pf.broker_connection_id, pf.external_account_id) == (
        "broker", rec.id, "fake-acc-1",
    )  # fmt: skip
    actions = [e.action for e in AuditLog(state).by_actor(alice)]
    assert "connection.connect" in actions
    assert "connection.link" in actions


def test_refused_credentials_are_not_stored(service, alice, state):
    with pytest.raises(ProviderAuthError):
        service.connect_with_keys(alice, "fake", {"token": "bad-token-xyz"})
    assert state.sql("SELECT COUNT(*) FROM broker_connections")[0][0] == 0
    assert state.sql("SELECT COUNT(*) FROM broker_credentials")[0][0] == 0


def test_missing_or_unknown_credential_fields_are_refused(service, alice):
    with pytest.raises(ConnectionsError, match="token"):
        service.connect_with_keys(alice, "fake", {})
    with pytest.raises(ConnectionsError, match="extra"):
        service.connect_with_keys(alice, "fake", {"token": TOKEN, "extra": "x"})


def test_services_cannot_own_connections(service):
    with pytest.raises(ConnectionsError):
        _connect(service, ADMIN)


def test_portal_provider_refuses_the_key_flow(service, alice):
    with pytest.raises(ConnectionsError, match="portal"):
        service.connect_with_keys(alice, "fake_portal", {"token": TOKEN})


# ---- tenant isolation -----------------------------------------------------------


def test_other_users_see_nothing(service, alice, bob):
    rec = _connect(service, alice)
    assert service.list(bob) == []
    for call in (
        lambda: service.get(bob, rec.id),
        lambda: service.accounts(bob, rec.id),
        lambda: service.sync(bob, rec.id),
        lambda: service.disconnect(bob, rec.id),
        lambda: service.link_account(bob, rec.id, "fake-acc-1"),
        lambda: service.complete_portal(bob, rec.id, "state"),
    ):
        with pytest.raises(NotFound):
            call()
    assert [c.id for c in service.list(alice)] == [rec.id]


def test_link_refuses_someone_elses_portfolio(service, alice, bob, state):
    rec = service.connect_with_keys(alice, "fake", {"token": TOKEN}, link_accounts=False)
    bobs = PortfolioRepository(state).create(bob, name="Bob broker", kind="broker")
    with pytest.raises(NotFound):
        service.link_account(alice, rec.id, "fake-acc-1", portfolio_id=bobs.id)
    # Even straight SQL can't link a portfolio to another user's connection.
    with pytest.raises(Exception, match="owner"):
        state.execute(
            "UPDATE portfolios SET broker_connection_id = ? WHERE id = ?", [rec.id, bobs.id]
        )


def test_an_account_links_to_one_portfolio_only(service, alice, state):
    rec = _connect(service, alice)
    other = PortfolioRepository(state).create(alice, name="Second", kind="broker")
    with pytest.raises(ConnectionsError, match="already linked"):
        service.link_account(alice, rec.id, "fake-acc-1", portfolio_id=other.id)


def test_link_refuses_simulated_portfolios_and_unknown_accounts(service, alice, state):
    rec = service.connect_with_keys(alice, "fake", {"token": TOKEN}, link_accounts=False)
    sim = PortfolioRepository(state).create(alice, name="Sim")
    with pytest.raises(ConnectionsError, match="broker"):
        service.link_account(alice, rec.id, "fake-acc-1", portfolio_id=sim.id)
    with pytest.raises(NotFound):
        service.link_account(alice, rec.id, "nope")


# ---- sync --------------------------------------------------------------------------


def test_sync_writes_a_sync_snapshot_positions_and_activities(service, alice, state):
    rec = _connect(service, alice)
    pf = _portfolio_for(service, alice, rec.id)
    result = service.sync(alice, rec.id)
    assert result.status == "ok"
    [p] = result.portfolios
    assert p.portfolio_id == pf
    assert p.unmapped == ("XYZ123",)
    assert p.activities_new == 2
    snap = state.sql("SELECT * FROM portfolio_snapshots WHERE portfolio_id = ?", [pf])
    assert len(snap) == 1
    assert snap[0]["source"] == "sync"
    assert snap[0]["as_of"] == "2026-03-03"
    assert snap[0]["tick_id"] is None
    assert json.loads(snap[0]["positions_json"]) == {"AAPL.US": 10, "BRK-B.US": 2}
    assert snap[0]["cash"] == 10_000.0
    assert snap[0]["total_value"] == 10_000.0 + 2000 + 900 + 30
    rows = state.sql(
        "SELECT raw_symbol, ticker, quantity FROM broker_positions WHERE snapshot_id = ?"
        " ORDER BY raw_symbol",
        [snap[0]["id"]],
    )
    assert [tuple(r) for r in rows] == [
        ("AAPL", "AAPL.US", 10.0),
        ("BRK.B", "BRK-B.US", 2.0),
        ("XYZ123", None, 3.0),  # not covered, kept
    ]
    acts = state.sql("SELECT * FROM broker_activities ORDER BY provider_activity_id")
    assert [(a["kind"], a["portfolio_id"]) for a in acts] == [("trade", pf), ("dividend", pf)]
    after = service.get(alice, rec.id)
    assert (after.status, after.last_sync_status, after.consecutive_failures) == ("active", "ok", 0)
    # Market hours: next sync in 15 minutes.
    assert after.next_sync_at == "2026-03-03T16:15:00+00:00"
    assert "connection.sync" in [e.action for e in AuditLog(state).by_actor(alice)]


def test_resync_is_idempotent(service, alice, state, clock):
    rec = _connect(service, alice)
    pf = _portfolio_for(service, alice, rec.id)
    service.sync(alice, rec.id)
    book = book_for(TOKEN)
    book.positions["fake-acc-1"] = [fake_position("AAPL", 12, 205.0)]
    clock.now += timedelta(minutes=20)
    again = service.sync(alice, rec.id)
    assert again.portfolios[0].activities_new == 0
    snaps = state.sql("SELECT * FROM portfolio_snapshots WHERE portfolio_id = ?", [pf])
    assert len(snaps) == 1  # same day: updated in place
    assert json.loads(snaps[0]["positions_json"]) == {"AAPL.US": 12}
    assert state.sql("SELECT COUNT(*) FROM broker_positions")[0][0] == 1
    assert state.sql("SELECT COUNT(*) FROM broker_activities")[0][0] == 2
    # Next trading day: a new snapshot; a new activity is added once.
    clock.now += timedelta(days=1)
    book.activities["fake-acc-1"].append(
        Activity("fake-act-3", "fake-acc-1", "fee", date(2026, 3, 4), amount=-1.0)
    )
    third = service.sync(alice, rec.id)
    assert third.portfolios[0].activities_new == 1
    assert (
        state.sql("SELECT COUNT(*) FROM portfolio_snapshots WHERE portfolio_id = ?", [pf])[0][0]
        == 2
    )


def test_sync_never_touches_tick_snapshots(service, alice, state):
    rec = _connect(service, alice)
    pf = _portfolio_for(service, alice, rec.id)
    state.execute(
        "INSERT INTO portfolio_snapshots (as_of, taken_at, cash, positions_json, total_value,"
        " portfolio_id) VALUES ('2026-03-03', 'x', 1, '{}', 1, ?)",
        [pf],
    )
    service.sync(alice, rec.id)
    sources = sorted(
        r[0]
        for r in state.sql("SELECT source FROM portfolio_snapshots WHERE portfolio_id = ?", [pf])
    )
    assert sources == ["sync", "tick"]


def test_auth_failure_marks_error_and_pauses_auto_subscriptions(service, alice, state):
    rec = _connect(service, alice)
    pf = _portfolio_for(service, alice, rec.id)
    _auto_subscription(state, alice.user_id, pf)
    book_for(TOKEN).fail = ProviderAuthError("token revoked")
    result = service.sync(alice, rec.id)
    assert result.status == "error"
    assert "revoked" in (result.error or "")
    after = service.get(alice, rec.id)
    assert (after.status, after.last_sync_status, after.consecutive_failures) == (
        "error", "error", 1,
    )  # fmt: skip
    sub = state.sql("SELECT paused_reason FROM subscriptions WHERE portfolio_id = ?", [pf])[0]
    assert sub["paused_reason"] == "broker_error"
    assert "connection.sync_failed" in [e.action for e in AuditLog(state).by_actor(alice)]
    # A scheduled pass skips it until the user reconnects; a manual sync may retry.
    assert service.sync_due(ADMIN) == []
    book_for(TOKEN).fail = None
    assert service.sync(alice, rec.id).status == "ok"
    assert service.get(alice, rec.id).status == "active"


def test_transient_failure_backs_off(service, alice, clock):
    rec = _connect(service, alice)
    book_for(TOKEN).fail = ProviderUnavailable("down")
    service.sync(alice, rec.id)
    first = service.get(alice, rec.id)
    assert first.status == "active"
    assert first.next_sync_at == "2026-03-03T16:01:00+00:00"
    service.sync(alice, rec.id)
    assert service.get(alice, rec.id).next_sync_at == "2026-03-03T16:02:00+00:00"
    book_for(TOKEN).fail = RateLimited("slow down", retry_after=600)
    service.sync(alice, rec.id)
    assert service.get(alice, rec.id).next_sync_at == "2026-03-03T16:10:00+00:00"


def test_missing_account_makes_a_partial_sync(service, alice, state):
    rec = _connect(service, alice)
    book = book_for(TOKEN)
    book.accounts = []
    result = service.sync(alice, rec.id)
    assert result.status == "partial"
    assert "fake-acc-1" in (result.portfolios[0].error or "")


def test_sync_due_runs_only_due_active_connections(service, alice, bob, clock):
    a = _connect(service, alice)
    b = _connect(service, bob, token="bob-token-987654")
    results = service.sync_due(ADMIN)
    assert sorted(r.connection_id for r in results) == sorted([a.id, b.id])
    assert service.sync_due(ADMIN) == []  # nothing due yet
    clock.now += timedelta(minutes=16)
    assert len(service.sync_due(ADMIN)) == 2
    with pytest.raises(ConnectionsError):
        service.sync_due(alice)  # scheduler only


def test_off_hours_sync_waits_an_hour(service, alice, clock):
    clock.now = clock.now.replace(hour=2)  # 21:00 New York the day before
    rec = _connect(service, alice)
    service.sync(alice, rec.id)
    assert service.get(alice, rec.id).next_sync_at == "2026-03-03T03:00:00+00:00"


def test_sync_of_a_disabled_provider_is_refused(state, box, alice, clock):
    on = ConnectionService(
        state, ConnectionsConfig(enabled_providers=("fake",)), box=box, clock=clock
    )
    rec = _connect(on, alice)
    off = ConnectionService(state, ConnectionsConfig(), box=box, clock=clock)
    with pytest.raises(ProviderDisabled):
        off.sync(alice, rec.id)
    assert off.sync_due(ADMIN) == []


# ---- hosted portal flow ---------------------------------------------------------------


def test_portal_flow(service, alice, state, clock):
    link = service.start_portal(alice, "fake_portal", "https://stonks.example/connections/callback")
    assert link.url.startswith("https://fake-broker.invalid/portal?")
    rec = service.get(alice, link.connection_id)
    assert rec.status == "pending"
    assert rec.external_user_id == f"stonks-{rec.id}"
    state_token = _state_from(link.url)
    stored = state.sql("SELECT pending_state_hash FROM broker_connections WHERE id = ?", [rec.id])[
        0
    ][0]
    assert state_token not in stored
    with pytest.raises(ConnectionsError, match="state"):
        service.complete_portal(alice, rec.id, "wrong-state")
    done = service.complete_portal(alice, rec.id, state_token)
    assert done.status == "active"
    assert service.accounts(alice, rec.id)[0].portfolio_id is not None
    with pytest.raises(ConnectionsError, match="state"):
        service.complete_portal(alice, rec.id, state_token)  # one-time


def test_portal_state_expires(service, alice, clock):
    link = service.start_portal(alice, "fake_portal", "https://stonks.example/cb")
    clock.now += timedelta(minutes=31)
    with pytest.raises(ConnectionsError, match="expired"):
        service.complete_portal(alice, link.connection_id, _state_from(link.url))


def test_portal_redirect_must_be_https(service, alice):
    with pytest.raises(ConnectionsError, match="https"):
        service.start_portal(alice, "fake_portal", "http://evil.example/cb")
    service.start_portal(alice, "fake_portal", "http://127.0.0.1:8000/cb")  # dev loopback


def test_api_key_provider_refuses_the_portal_flow(service, alice):
    with pytest.raises(ConnectionsError, match="API key"):
        service.start_portal(alice, "fake", "https://stonks.example/cb")


def test_abandoned_portal_stays_pending(service, alice):
    link = service.start_portal(alice, "fake_portal", "https://stonks.example/cb")
    rec = service.complete_portal(
        alice, link.connection_id, _state_from(link.url), outcome="ABANDONED"
    )
    assert rec.status == "pending"
    assert "not completed" in (rec.last_error or "")


# ---- disconnect -----------------------------------------------------------------


def test_disconnect_removes_credentials_and_activities_and_archives(service, alice, state):
    link = service.start_portal(alice, "fake_portal", "https://stonks.example/cb")
    rec = service.complete_portal(alice, link.connection_id, _state_from(link.url))
    pf = _portfolio_for(service, alice, rec.id)
    service.sync(alice, rec.id)
    token = f"portal-stonks-{rec.id}"
    assert f"stonks-{rec.id}" in book_for(token).registered_users
    result = service.disconnect(alice, rec.id)
    assert result.archived_portfolios == (pf,)
    assert result.remote_removed is True
    assert f"stonks-{rec.id}" not in book_for(token).registered_users
    for table in (
        "broker_connections",
        "broker_credentials",
        "broker_accounts",
        "broker_activities",
    ):
        assert state.sql(f"SELECT COUNT(*) FROM {table}")[0][0] == 0
    kept = PortfolioRepository(state).get(alice, pf)
    assert (kept.status, kept.broker_connection_id) == ("archived", None)
    # Snapshots of the archived portfolio are kept.
    assert (
        state.sql("SELECT COUNT(*) FROM portfolio_snapshots WHERE portfolio_id = ?", [pf])[0][0]
        == 1
    )
    assert "connection.disconnect" in [e.action for e in AuditLog(state).by_actor(alice)]


def test_disconnect_survives_a_remote_failure(service, alice, state, monkeypatch):
    link = service.start_portal(alice, "fake_portal", "https://stonks.example/cb")
    rec = service.complete_portal(alice, link.connection_id, _state_from(link.url))

    def boom(*a, **k):
        raise ProviderUnavailable("provider down")

    monkeypatch.setattr(fake.FakePortalConnection, "unregister_user", classmethod(boom))
    result = service.disconnect(alice, rec.id)
    assert result.remote_removed is False
    assert "provider down" in (result.remote_error or "")
    assert service.list(alice) == []


def test_disconnect_survives_an_unexpected_remote_bug(service, alice, monkeypatch):
    link = service.start_portal(alice, "fake_portal", "https://stonks.example/cb")
    rec = service.complete_portal(alice, link.connection_id, _state_from(link.url))
    secret = f"portal-stonks-{rec.id}"

    def boom(*a, **k):
        raise RuntimeError(f"vendor bug with {secret}")

    monkeypatch.setattr(fake.FakePortalConnection, "unregister_user", classmethod(boom))
    result = service.disconnect(alice, rec.id)
    assert result.remote_removed is False
    assert secret not in (result.remote_error or "")
    assert "RuntimeError" in (result.remote_error or "")


# ---- secrets hygiene ---------------------------------------------------------------


def test_unexpected_provider_errors_fail_one_sync_without_leaking(service, alice, bob):
    a = _connect(service, alice)
    b = _connect(service, bob, token="bob-token-987654")
    book_for(TOKEN).fail = RuntimeError(f"KeyError near {TOKEN}")  # type: ignore[assignment]
    results = {r.connection_id: r for r in service.sync_due(ADMIN)}
    assert results[a.id].status == "error"
    assert TOKEN not in (results[a.id].error or "")
    assert "RuntimeError" in (results[a.id].error or "")
    assert results[b.id].status == "ok"  # one broken connection doesn't stop the pass


def test_unexpected_errors_while_connecting_do_not_leak(service, alice, monkeypatch):
    def boom(self):
        raise RuntimeError(f"bad parse of {TOKEN}")

    monkeypatch.setattr(fake.FakeConnection, "accounts", boom)
    with pytest.raises(ConnectionsError) as info:
        _connect(service, alice)
    assert TOKEN not in str(info.value)
    assert info.value.__cause__ is None


def test_sync_due_skips_disabled_users(service, alice, state):
    from stonks.accounts import UserRepository

    _connect(service, alice)
    UserRepository(state).set_status(alice.user_id, "disabled", actor="t")
    assert service.sync_due(ADMIN) == []


def test_no_secret_reaches_the_database_in_clear(service, alice, state):
    rec = _connect(service, alice)
    book_for(TOKEN).fail = ProviderAuthError(f"token {TOKEN} was revoked")
    result = service.sync(alice, rec.id)
    assert TOKEN not in (result.error or "")
    dump = "\n".join(state.con.iterdump())
    assert TOKEN not in dump


def test_credentials_need_the_right_key(service, alice, state, clock):
    rec = _connect(service, alice)
    other = SecretBox(KeyRing.parse(f"k1:{generate_key()}"))
    wrong = ConnectionService(state, service.config, box=other, clock=clock)
    result = wrong.sync(alice, rec.id)
    assert result.status == "error"
    assert "credentials" in (result.error or "")


def test_rotate_credentials_rewraps_under_the_new_key(service, alice, state, key, clock):
    rec = _connect(service, alice)
    rotated_box = SecretBox(KeyRing.parse(f"k2:{generate_key()},k1:{key}"))
    rotated = ConnectionService(state, service.config, box=rotated_box, clock=clock)
    with pytest.raises(ConnectionsError):
        rotated.rotate_credentials(alice)
    assert rotated.rotate_credentials(ADMIN) == 1
    assert rotated.rotate_credentials(ADMIN) == 0
    row = state.sql("SELECT key_id, rotated_at FROM broker_credentials")[0]
    assert row["key_id"] == "k2" and row["rotated_at"]
    assert rotated.sync(alice, rec.id).status == "ok"


def test_list_never_needs_the_master_key(state, config, alice, clock, box):
    rec = _connect(ConnectionService(state, config, box=box, clock=clock), alice)
    keyless = ConnectionService(state, config, box=None, clock=clock)
    assert [c.id for c in keyless.list(alice)] == [rec.id]


# ---- helpers -----------------------------------------------------------------------


def _state_from(url: str) -> str:
    from urllib.parse import parse_qs, unquote, urlsplit

    redirect = unquote(parse_qs(urlsplit(url).query)["redirect"][0])
    return parse_qs(urlsplit(redirect).query)["state"][0]


def _auto_subscription(state, user_id: str, portfolio_id: str) -> None:
    now = "2026-03-01T00:00:00+00:00"
    state.execute(
        "INSERT INTO strategies (id, class_path, params_json, status, created_at, updated_at)"
        " VALUES ('s1', 'm:C', '{}', 'shadow', ?, ?)",
        [now, now],
    )
    state.execute(
        "INSERT INTO subscriptions (id, user_id, strategy_id, portfolio_id, mode, created_at,"
        " updated_at) VALUES ('sub_1', ?, 's1', ?, 'auto', ?, ?)",
        [user_id, portfolio_id, now, now],
    )
