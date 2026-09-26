"""Router -> outbox -> worker -> the real WebPushChannel (real encryption and
VAPID signing), with a fake HTTP session standing in for the push service."""

from __future__ import annotations

import base64
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from stonks.notify import service
from stonks.notify.events import Audience, Event
from stonks.notify.router import NotificationRouter
from stonks.notify.settings import NotifySettings, OutboxSettings, WebPushSettings
from stonks.notify.webpush import WebPushChannel, generate_vapid_keys
from stonks.notify.worker import DeliveryWorker

ENDPOINT = "https://fcm.googleapis.com/fcm/send/SECRET-DEVICE"


class _Resp:
    def __init__(self, status: int) -> None:
        self.status_code = status
        self.reason = "X"
        self.headers: dict = {}
        self.text = f"see {ENDPOINT}"


class Session:
    def __init__(self) -> None:
        self.statuses: list[int] = []
        self.posts: list[str] = []

    def post(self, url, timeout=None, data=None, headers=None):
        self.posts.append(url)
        return _Resp(self.statuses.pop(0) if self.statuses else 201)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@pytest.fixture
def session():
    return Session()


@pytest.fixture
def setup(state, clock, alice_scope, session):
    keys = generate_vapid_keys()
    settings = NotifySettings(
        outbox=OutboxSettings(max_attempts=10, push_failure_limit=3, backoff_base_seconds=1),
        webpush=WebPushSettings(
            public_key=keys.public_key,
            private_key=keys.private_key,
            subject="mailto:ops@example.com",
        ),
    )
    channel = WebPushChannel.from_settings(settings)
    assert channel is not None
    channel._session = session
    pub = ec.generate_private_key(ec.SECP256R1()).public_key()
    device = service.register_push_subscription(
        state,
        alice_scope,
        endpoint=ENDPOINT,
        p256dh=_b64(pub.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)),
        auth=_b64(b"k" * 16),
    )
    channels = {"webpush": channel}
    router = NotificationRouter(state, channels, settings.outbox, clock=clock)
    worker = DeliveryWorker(state, channels, settings.outbox, clock=clock)
    return router, worker, device


def _publish(router, user_id):
    router.publish(
        Event(category="order", title="Filled", body="x", audience=Audience.users(user_id))
    )


def test_delivers_and_records_success(state, setup, session, users):
    router, worker, device = setup
    _publish(router, users["alice"].id)
    assert worker.run_once().sent == 1
    assert session.posts == [ENDPOINT]
    [row] = state.sql("SELECT last_success_at, failure_count FROM push_subscriptions")
    assert row["last_success_at"] and row["failure_count"] == 0


@pytest.mark.parametrize("status", [404, 410])
def test_gone_revokes_the_device(state, setup, session, users, alice_scope, status):
    router, worker, _ = setup
    session.statuses = [status]
    _publish(router, users["alice"].id)
    assert worker.run_once().dead == 1
    assert service.list_push_subscriptions(state, alice_scope) == []
    [row] = state.sql("SELECT revoked_reason FROM push_subscriptions")
    assert row["revoked_reason"] == "gone"
    [d] = state.sql("SELECT last_error FROM notification_deliveries")
    assert "SECRET-DEVICE" not in d["last_error"]
    # Nothing more is queued for a revoked device.
    _publish(router, users["alice"].id)
    assert state.sql("SELECT COUNT(*) FROM notification_deliveries")[0][0] == 1


def test_consecutive_failures_disable_the_device_and_tell_the_user(
    state, setup, session, users, alice_scope, clock
):
    router, worker, _ = setup
    alice = users["alice"].id
    session.statuses = [500, 500, 500]
    _publish(router, alice)
    for _ in range(3):
        worker.run_once()
        clock.now += timedelta(minutes=5)
    assert service.list_push_subscriptions(state, alice_scope) == []
    [row] = state.sql("SELECT revoked_reason, failure_count FROM push_subscriptions")
    assert row["revoked_reason"] == "failures" and row["failure_count"] == 3
    feed = service.list_notifications(state, alice_scope)
    assert any("stopped" in n.title for n in feed)
    # The queued delivery is skipped now that its device is gone.
    assert worker.run_once().skipped == 1


def test_a_success_resets_the_failure_count(state, setup, session, users, clock):
    router, worker, _ = setup
    session.statuses = [500, 500, 201]
    _publish(router, users["alice"].id)
    for _ in range(3):
        worker.run_once()
        clock.now += timedelta(minutes=5)
    [row] = state.sql("SELECT failure_count, revoked_at FROM push_subscriptions")
    assert row["failure_count"] == 0 and row["revoked_at"] is None
