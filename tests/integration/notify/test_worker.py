"""DeliveryWorker: claims due deliveries, applies quiet hours (with the
urgent bypass and a morning digest), retries with backoff, dead-letters,
and recovers rows a crashed worker left claimed."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from stonks.notify.channels import DeliveryResult
from stonks.notify.events import Audience, Event
from stonks.notify.prefs import PreferenceStore
from stonks.notify.router import NotificationRouter, iso
from stonks.notify.settings import OutboxSettings
from stonks.notify.worker import DeliveryWorker

from .fakes import FakeChannel, FakePush, add_device

SETTINGS = OutboxSettings(max_attempts=3, backoff_base_seconds=30, backoff_max_seconds=600)


@pytest.fixture
def push():
    return FakePush()


@pytest.fixture
def router(state, clock, push):
    return NotificationRouter(state, {"webpush": push}, SETTINGS, clock=clock)


@pytest.fixture
def worker(state, clock, push):
    return DeliveryWorker(state, {"webpush": push}, SETTINGS, clock=clock)


@pytest.fixture
def alice(state, users):
    uid = users["alice"].id
    add_device(state, uid, "psh_1")
    return uid


def _publish(router, user_id, **kw):
    base = {"category": "order", "title": "Order filled", "body": "1 order filled"}
    base.update(kw)
    return router.publish(Event(audience=Audience.users(user_id), **base))


def _rows(state):
    return [dict(r) for r in state.sql("SELECT * FROM notification_deliveries ORDER BY id")]


def test_sends_due_deliveries_and_marks_them_sent(state, router, worker, push, alice, clock):
    _publish(router, alice, deep_link="/orders", dedupe_key="order:o1")
    stats = worker.run_once()
    assert stats.sent == 1
    [(msg, target)] = push.sent
    assert target == "psh_1"
    assert msg.title == "Order filled" and msg.deep_link == "/orders"
    assert msg.dedupe_key == "order:o1" and msg.ttl_seconds == 24 * 3600
    [row] = _rows(state)
    assert row["status"] == "sent" and row["attempts"] == 1 and row["sent_at"] == iso(clock())
    assert push.ok == [(alice, "psh_1")]
    assert worker.run_once().sent == 0  # nothing twice


def test_retry_with_exponential_backoff_then_dead_letter(state, router, worker, push, alice, clock):
    _publish(router, alice)
    push.outcomes.extend([DeliveryResult.retry("503")] * 3)
    assert worker.run_once().retried == 1
    [row] = _rows(state)
    assert row["status"] == "failed" and row["attempts"] == 1
    assert row["next_attempt_at"] == iso(clock() + timedelta(seconds=30))
    assert row["last_error"] == "503"
    assert worker.run_once().retried == 0  # not due yet
    clock.now += timedelta(seconds=30)
    worker.run_once()
    assert _rows(state)[0]["next_attempt_at"] == iso(clock() + timedelta(seconds=60))
    clock.now += timedelta(seconds=60)
    stats = worker.run_once()
    assert stats.dead == 1
    row = _rows(state)[0]
    assert row["status"] == "dead" and row["attempts"] == 3


def test_backoff_is_capped_and_honours_retry_after(state, clock, alice, push):
    settings = OutboxSettings(max_attempts=10, backoff_base_seconds=30, backoff_max_seconds=100)
    router = NotificationRouter(state, {"webpush": push}, settings, clock=clock)
    worker = DeliveryWorker(state, {"webpush": push}, settings, clock=clock)
    _publish(router, alice)
    push.outcomes.append(DeliveryResult.retry("429", after=90))
    worker.run_once()
    assert _rows(state)[0]["next_attempt_at"] == iso(clock() + timedelta(seconds=90))
    state.execute(
        "UPDATE notification_deliveries SET attempts = 8, next_attempt_at = ?", [iso(clock())]
    )
    push.outcomes.append(DeliveryResult.retry("503"))
    worker.run_once()
    assert _rows(state)[0]["next_attempt_at"] == iso(clock() + timedelta(seconds=100))


def test_dead_and_gone_outcomes_call_the_channel_hook(state, router, worker, push, alice):
    _publish(router, alice)
    push.outcomes.append(DeliveryResult.gone("410"))
    stats = worker.run_once()
    assert stats.dead == 1
    assert push.failed == [(alice, "psh_1", "gone")]
    assert _rows(state)[0]["status"] == "dead"


def test_a_raising_channel_is_a_retry_not_a_crash(state, router, worker, push, alice):
    _publish(router, alice)
    push.outcomes.append(RuntimeError("boom"))
    assert worker.run_once().retried == 1
    assert "RuntimeError" in _rows(state)[0]["last_error"]


def test_removed_target_is_skipped(state, router, worker, push, alice):
    _publish(router, alice)
    state.execute("UPDATE push_subscriptions SET revoked_at = 'x'")
    assert worker.run_once().skipped == 1
    assert push.sent == []
    assert _rows(state)[0]["status"] == "skipped"


def test_unconfigured_channel_retries_then_dies(state, clock, alice, push):
    router = NotificationRouter(state, {"webpush": push}, SETTINGS, clock=clock)
    worker = DeliveryWorker(state, {}, SETTINGS, clock=clock)  # webpush lost its keys
    _publish(router, alice)
    worker.run_once()
    assert _rows(state)[0]["status"] == "failed"
    assert "not configured" in _rows(state)[0]["last_error"]


def test_stale_claim_is_recovered_after_the_lease(state, router, worker, push, alice, clock):
    _publish(router, alice)
    # A worker claimed the row and crashed.
    state.execute(
        "UPDATE notification_deliveries SET status = 'sending', next_attempt_at = ?",
        [iso(clock() + timedelta(seconds=120))],
    )
    assert worker.run_once().sent == 0
    clock.now += timedelta(seconds=121)
    assert worker.run_once().sent == 1


def test_claim_is_exclusive(state, router, push, alice, clock, tmp_path):
    _publish(router, alice)
    w = DeliveryWorker(state, {"webpush": push}, SETTINGS, clock=clock)
    first = w._claim(clock())
    second = w._claim(clock())
    assert len(first) == 1 and second == []


# ---- quiet hours ---------------------------------------------------------------


def _quiet(state, user_id, tz="America/New_York", start="22:00", end="07:00"):
    state.execute("UPDATE users SET timezone = ? WHERE id = ?", [tz, user_id])
    PreferenceStore(state).set_quiet_hours(user_id, start, end, now=datetime.now(UTC))


def test_quiet_hours_defer_normal_until_the_window_ends(state, router, worker, push, alice, clock):
    _quiet(state, alice)
    clock.now = datetime(2026, 1, 6, 4, 0, tzinfo=UTC)  # 23:00 in New York
    _publish(router, alice)
    stats = worker.run_once()
    assert stats.deferred == 1 and push.sent == []
    [row] = _rows(state)
    assert row["status"] == "deferred" and row["deferred"] == 1 and row["attempts"] == 0
    assert row["next_attempt_at"] == "2026-01-06T12:00:00+00:00"  # 07:00 New York
    clock.now = datetime(2026, 1, 6, 12, 0, tzinfo=UTC)
    assert worker.run_once().sent == 1
    assert push.sent[0][0].title == "Order filled"  # a single one isn't a digest


def test_high_urgency_bypasses_quiet_hours(state, router, worker, push, alice, clock):
    _quiet(state, alice)
    clock.now = datetime(2026, 1, 6, 4, 0, tzinfo=UTC)
    _publish(router, alice, category="risk", title="Trading halted")
    assert worker.run_once().sent == 1


def test_quiet_hours_use_the_users_zone_not_utc(state, router, worker, push, alice, clock):
    _quiet(state, alice)
    clock.now = datetime(2026, 1, 5, 23, 0, tzinfo=UTC)  # 18:00 in New York: not quiet
    _publish(router, alice)
    assert worker.run_once().sent == 1


def test_deferred_backlog_goes_out_as_one_digest(state, router, worker, push, alice, clock):
    _quiet(state, alice)
    clock.now = datetime(2026, 1, 6, 4, 0, tzinfo=UTC)
    for i in range(3):
        _publish(router, alice, title=f"Order {i} filled")
    assert worker.run_once().deferred == 3
    clock.now = datetime(2026, 1, 6, 12, 0, tzinfo=UTC)
    stats = worker.run_once()
    assert stats.sent == 3
    [(msg, target)] = push.sent
    assert msg.digest_count == 3 and "3 notifications" in msg.title
    assert msg.deep_link == "/notifications"
    assert {r["status"] for r in _rows(state)} == {"sent"}


def test_digest_failure_applies_to_every_row(state, router, worker, push, alice, clock):
    _quiet(state, alice)
    clock.now = datetime(2026, 1, 6, 4, 0, tzinfo=UTC)
    _publish(router, alice)
    _publish(router, alice)
    worker.run_once()
    clock.now = datetime(2026, 1, 6, 12, 0, tzinfo=UTC)
    push.outcomes.append(DeliveryResult.retry("503"))
    assert worker.run_once().retried == 2
    assert {r["status"] for r in _rows(state)} == {"failed"}


def test_changing_quiet_hours_affects_queued_rows(state, router, worker, push, alice, clock):
    clock.now = datetime(2026, 1, 6, 4, 0, tzinfo=UTC)
    _publish(router, alice)
    _quiet(state, alice)  # set after enqueue, before delivery
    assert worker.run_once().deferred == 1


def test_digest_is_per_channel_and_target(state, clock, users):
    alice = users["alice"].id
    add_device(state, alice, "psh_1")
    add_device(state, alice, "psh_2")
    push = FakePush()
    router = NotificationRouter(state, {"webpush": push}, SETTINGS, clock=clock)
    worker = DeliveryWorker(state, {"webpush": push}, SETTINGS, clock=clock)
    _quiet(state, alice)
    clock.now = datetime(2026, 1, 6, 4, 0, tzinfo=UTC)
    _publish(router, alice)
    _publish(router, alice)
    worker.run_once()
    clock.now = datetime(2026, 1, 6, 12, 0, tzinfo=UTC)
    worker.run_once()
    assert sorted(t for _, t in push.sent) == ["psh_1", "psh_2"]
    assert all(m.digest_count == 2 for m, _ in push.sent)


def test_single_target_channels_work_too(state, clock, users):
    alice = users["alice"].id
    hook = FakeChannel("webhook", default_enabled=True)
    router = NotificationRouter(state, {"webhook": hook}, SETTINGS, clock=clock)
    worker = DeliveryWorker(state, {"webhook": hook}, SETTINGS, clock=clock)
    _publish(router, alice)
    assert worker.run_once().sent == 1
    assert hook.sent[0][1] == f"webhook:{alice}"


def test_run_forever_stops_on_the_event(state, clock, push):
    worker = DeliveryWorker(state, {"webpush": push}, SETTINGS, clock=clock)
    stop = threading.Event()
    stop.set()
    started = time.monotonic()
    worker.run_forever(stop, interval_seconds=10.0)  # returns at once
    assert time.monotonic() - started < 5.0
    assert push.sent == []
