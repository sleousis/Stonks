"""Web Push and notification routes: VAPID key, browser subscriptions,
preferences, quiet hours, the user's webhook (write-only) and the in-app
feed. Scoped to the bootstrap admin until S2."""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from stonks.accounts import DEFAULT_OWNER_ID, Role, UserRepository
from stonks.api import create_app
from stonks.app.context import AppContext
from stonks.app.notifications import NotificationsAppService
from stonks.app.services import Services
from stonks.notify.settings import NotifySettings, OutboxSettings, WebPushSettings
from stonks.notify.webpush import generate_vapid_keys
from stonks.store.state import SqliteState
from tests.integration.app.test_api import AUTH, LOOPBACK, REMOTE

KEYS = generate_vapid_keys()
ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc123"
P256DH = base64.urlsafe_b64encode(b"\x04" + b"\x01" * 64).rstrip(b"=").decode()
AUTH_KEY = base64.urlsafe_b64encode(b"\x02" * 16).rstrip(b"=").decode()
HOOK = "https://hooks.example.com/services/T000/B000/secretpath"


def _notify_settings(configured: bool = True) -> NotifySettings:
    webpush = (
        WebPushSettings(
            public_key=KEYS.public_key,
            private_key=KEYS.private_key,
            subject="mailto:ops@example.com",
        )
        if configured
        else WebPushSettings(public_key=None, private_key=None, subject=None)
    )
    return NotifySettings(outbox=OutboxSettings(), webpush=webpush)


@pytest.fixture
def services(settings, seeded, fake_source):
    settings.api.allowed_hosts = ["testserver"]
    ctx = AppContext(settings, source_factory=lambda: fake_source)
    svc = Services.create(ctx)
    svc.notifications = NotificationsAppService(ctx, settings=_notify_settings())
    return svc


@pytest.fixture
def client(settings, services):
    with TestClient(create_app(settings, services=services), client=LOOPBACK) as c:
        yield c


def _subscribe(client, endpoint=ENDPOINT):
    return client.post(
        "/api/push/subscriptions",
        json={
            "endpoint": endpoint,
            "keys": {"p256dh": P256DH, "auth": AUTH_KEY},
            "user_agent": "Chrome on Windows",
        },
        headers=AUTH,
    )


def _alert(settings, title: str, user_id: str = DEFAULT_OWNER_ID) -> int:
    with SqliteState(settings.state.path) as state:
        cur = state.execute(
            "INSERT INTO alerts (level, title, message, context_json, created_at, user_id,"
            " category) VALUES ('info', ?, 'msg', '{\"deep_link\": \"/orders\"}',"
            " '2026-09-26T10:00:00+00:00', ?, 'order')",
            [title, user_id],
        )
        return int(cur.lastrowid)


# ---- VAPID key ---------------------------------------------------------------------


def test_vapid_key_is_public_to_token_holders(client):
    resp = client.get("/api/push/vapid-key", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"public_key": KEYS.public_key}
    assert KEYS.private_key not in resp.text


def test_vapid_key_is_null_when_push_is_not_configured(settings, services):
    services.notifications = NotificationsAppService(
        services.context, settings=_notify_settings(configured=False)
    )
    with TestClient(create_app(settings, services=services), client=LOOPBACK) as c:
        assert c.get("/api/push/vapid-key", headers=AUTH).json() == {"public_key": None}


# ---- subscriptions -------------------------------------------------------------------


def test_register_list_and_remove_a_browser(client):
    resp = _subscribe(client)
    assert resp.status_code == 201, resp.text
    device = resp.json()
    assert device["endpoint_host"] == "fcm.googleapis.com"
    # Endpoints are capability URLs and keys are secrets: never echoed.
    assert ENDPOINT not in resp.text and P256DH not in resp.text and AUTH_KEY not in resp.text

    listed = client.get("/api/push/subscriptions", headers=AUTH)
    assert [d["id"] for d in listed.json()] == [device["id"]]
    assert ENDPOINT not in listed.text

    removed = client.request(
        "DELETE", "/api/push/subscriptions", json={"endpoint": ENDPOINT}, headers=AUTH
    )
    assert removed.status_code == 204
    assert client.get("/api/push/subscriptions", headers=AUTH).json() == []
    again = client.request(
        "DELETE", "/api/push/subscriptions", json={"endpoint": ENDPOINT}, headers=AUTH
    )
    assert again.status_code == 404


def test_a_device_is_removed_by_its_id(client, settings):
    device = _subscribe(client).json()
    path = f"/api/push/subscriptions/{device['id']}"
    assert client.delete(path, headers=AUTH).status_code == 204
    assert client.get("/api/push/subscriptions", headers=AUTH).json() == []
    assert client.delete(path, headers=AUTH).status_code == 404


def test_another_users_device_reads_as_missing(client, settings):
    device = _subscribe(client).json()
    with SqliteState(settings.state.path) as state:
        other = UserRepository(state).create(display_name="Bob", role=Role.TRADER, actor="t")
        state.execute(
            "UPDATE push_subscriptions SET user_id = ? WHERE id = ?", [other.id, device["id"]]
        )
    resp = client.delete(f"/api/push/subscriptions/{device['id']}", headers=AUTH)
    assert resp.status_code == 404


def test_unknown_push_service_is_refused(client):
    resp = _subscribe(client, endpoint="https://evil.example.com/push")
    assert resp.status_code == 422


def test_push_routes_need_the_token(client, settings, services):
    assert client.get("/api/push/vapid-key").status_code == 401
    assert _subscribe_no_auth(client).status_code == 401
    with TestClient(create_app(settings, services=services), client=REMOTE) as remote:
        assert _subscribe_no_auth(remote).status_code == 401


def _subscribe_no_auth(client):
    return client.post(
        "/api/push/subscriptions",
        json={"endpoint": ENDPOINT, "keys": {"p256dh": P256DH, "auth": AUTH_KEY}},
    )


# ---- preferences, quiet hours, webhook -------------------------------------------------


def test_preferences_round_trip(client):
    got = client.get("/api/notifications/preferences", headers=AUTH)
    assert got.status_code == 200
    body = got.json()
    assert "webpush" in body["channels"]
    assert body["webhook"] is None

    updated = client.put(
        "/api/notifications/preferences",
        json={"preferences": [{"category": "signal", "channel": "webpush", "enabled": False}]},
        headers=AUTH,
    )
    assert updated.status_code == 200, updated.text
    prefs = updated.json()["preferences"]
    assert {"category": "signal", "channel": "webpush", "enabled": False, "strategy_id": None} in (
        prefs
    )
    bad = client.put(
        "/api/notifications/preferences",
        json={
            "preferences": [{"category": "signal", "channel": "carrier-pigeon", "enabled": True}]
        },
        headers=AUTH,
    )
    assert bad.status_code == 422


def test_quiet_hours(client):
    resp = client.put(
        "/api/notifications/quiet-hours", json={"start": "22:00", "end": "07:00"}, headers=AUTH
    )
    assert resp.status_code == 200, resp.text
    assert (resp.json()["quiet_start"], resp.json()["quiet_end"]) == ("22:00", "07:00")
    bad = client.put(
        "/api/notifications/quiet-hours", json={"start": "25:00", "end": "07:00"}, headers=AUTH
    )
    assert bad.status_code == 422
    cleared = client.put(
        "/api/notifications/quiet-hours", json={"start": None, "end": None}, headers=AUTH
    )
    assert cleared.json()["quiet_start"] is None


def test_webhook_is_write_only(client, settings):
    resp = client.put("/api/notifications/webhook", json={"url": HOOK}, headers=AUTH)
    assert resp.status_code == 200, resp.text
    assert "secretpath" not in resp.text
    assert resp.json()["webhook"] == "https://hooks.example.com/***"
    got = client.get("/api/notifications/preferences", headers=AUTH)
    assert "secretpath" not in got.text
    with SqliteState(settings.state.path) as state:
        audit = state.sql("SELECT details_json FROM audit_log WHERE action = 'notify.webhook'")
    assert audit and "secretpath" not in str([r[0] for r in audit])
    bad = client.put(
        "/api/notifications/webhook", json={"url": "http://127.0.0.1/hook"}, headers=AUTH
    )
    assert bad.status_code == 422
    assert (
        client.put("/api/notifications/webhook", json={"url": None}, headers=AUTH).json()["webhook"]
        is None
    )


# ---- feed ------------------------------------------------------------------------------


def test_feed_unread_count_and_mark_read(client, settings):
    first = _alert(settings, "filled AAPL")
    second = _alert(settings, "filled MSFT")
    with SqliteState(settings.state.path) as state:
        other = UserRepository(state).create(display_name="Bob", role=Role.TRADER, actor="t")
    theirs = _alert(settings, "bob's order", user_id=other.id)

    feed = client.get("/api/notifications", headers=AUTH)
    assert feed.status_code == 200
    body = feed.json()
    assert [i["id"] for i in body["items"]] == [second, first]
    assert body["unread_count"] == 2
    assert body["items"][0]["deep_link"] == "/orders"

    marked = client.post("/api/notifications/read", json={"ids": [first, theirs]}, headers=AUTH)
    assert marked.status_code == 200
    assert marked.json() == {"updated": 1, "unread_count": 1}
    unread = client.get("/api/notifications", params={"unread_only": True}, headers=AUTH).json()
    assert [i["id"] for i in unread["items"]] == [second]

    all_read = client.post("/api/notifications/read", json={}, headers=AUTH)
    assert all_read.json() == {"updated": 1, "unread_count": 0}
    page = client.get(
        "/api/notifications", params={"before_id": second, "limit": 1}, headers=AUTH
    ).json()
    assert [i["id"] for i in page["items"]] == [first]
    assert client.get("/api/notifications", params={"limit": 0}, headers=AUTH).status_code == 422
