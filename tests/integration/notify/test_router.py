"""NotificationRouter: audience resolution, the in-app row, preferences,
fallbacks, dedupe (with and without a window) and redaction."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from stonks.accounts import PortfolioRepository, Scope, UserRepository
from stonks.notify.events import Audience, Event
from stonks.notify.prefs import Preference, PreferenceStore
from stonks.notify.router import NotificationRouter, SignalNotice, notify_signal, notify_signals
from stonks.notify.settings import OutboxSettings

from .fakes import FakeChannel, FakePush, add_device

NOW_ISO = "2026-01-05T15:00:00+00:00"


@pytest.fixture
def push():
    return FakePush()


@pytest.fixture
def webhook():
    return FakeChannel("webhook", fallback=True)


@pytest.fixture
def email():
    return FakeChannel("email", fallback=True)


@pytest.fixture
def router(state, clock, push, webhook, email):
    return NotificationRouter(
        state,
        {"webpush": push, "webhook": webhook, "email": email},
        OutboxSettings(),
        clock=clock,
    )


def _deliveries(state, user_id=None):
    sql = "SELECT * FROM notification_deliveries"
    params = []
    if user_id:
        sql += " WHERE user_id = ?"
        params.append(user_id)
    return [dict(r) for r in state.sql(sql + " ORDER BY id", params)]


def _event(audience, **kw):
    base = {"category": "order", "title": "Order filled", "body": "1 order filled"}
    base.update(kw)
    return Event(audience=audience, **base)


def test_writes_outbox_alert_and_one_delivery_per_device(state, users, router):
    alice = users["alice"].id
    add_device(state, alice, "psh_1")
    add_device(state, alice, "psh_2")
    result = router.publish(_event(Audience.users(alice), deep_link="/orders"))
    [nid] = result.notification_ids
    [row] = state.sql("SELECT * FROM notification_outbox WHERE id = ?", [nid])
    assert row["user_id"] == alice and row["category"] == "order"
    assert row["urgency"] == "normal" and row["deep_link"] == "/orders"
    [alert] = state.sql("SELECT * FROM alerts WHERE id = ?", [row["alert_id"]])
    assert alert["user_id"] == alice and alert["category"] == "order"
    assert alert["title"] == "Order filled" and alert["read_at"] is None
    assert json.loads(alert["context_json"]) == {"deep_link": "/orders", "notification_id": nid}
    ds = _deliveries(state, alice)
    assert [(d["channel"], d["target_id"], d["status"]) for d in ds] == [
        ("webpush", "psh_1", "pending"),
        ("webpush", "psh_2", "pending"),
    ]
    assert all(d["next_attempt_at"] == NOW_ISO for d in ds)
    assert result.deliveries == 2


def test_no_devices_no_push_rows_but_inapp_always(state, users, router):
    alice = users["alice"].id
    result = router.publish(_event(Audience.users(alice)))
    assert len(result.notification_ids) == 1
    assert _deliveries(state) == []
    assert state.sql("SELECT COUNT(*) FROM alerts WHERE user_id = ?", [alice])[0][0] == 1


def test_owner_audience_reaches_only_the_portfolio_owner(state, users, router):
    alice, bob = users["alice"].id, users["bob"].id
    pf = PortfolioRepository(state).create(Scope.for_user(users["bob"]), name="Bob's")
    result = router.publish(_event(Audience.owner_of(pf.id), portfolio_id=pf.id))
    assert len(result.notification_ids) == 1
    owners = {r["user_id"] for r in state.sql("SELECT user_id FROM notification_outbox")}
    assert owners == {bob}
    assert alice not in owners
    assert router.publish(_event(Audience.owner_of("pf_missing"))).notification_ids == ()


def test_admins_audience_expands_to_each_active_admin(state, users, router):
    result = router.publish(_event(Audience.admins(), category="system"))
    rows = state.sql("SELECT user_id FROM notification_outbox")
    # usr_owner (migration default) and Ada are the admins.
    assert {r["user_id"] for r in rows} == {"usr_owner", users["admin"].id}
    assert len(result.notification_ids) == 2


def test_disabled_and_service_users_are_skipped(state, users, router):
    alice = users["alice"].id
    UserRepository(state).set_status(alice, "disabled", actor="service:system")
    result = router.publish(_event(Audience.users(alice, "usr_nobody")))
    assert result.notification_ids == ()


def test_subscribers_audience_uses_enabled_notify_subscriptions(state, users, router):
    alice, bob = users["alice"].id, users["bob"].id
    state.execute(
        "INSERT INTO strategies (id, class_path, params_json, status, created_at, updated_at)"
        " VALUES ('s1', 'm:C', '{}', 'shadow', ?, ?)",
        [NOW_ISO, NOW_ISO],
    )
    for sub, uid, enabled in [("sub_a", alice, 1), ("sub_b", bob, 0)]:
        state.execute(
            "INSERT INTO subscriptions (id, user_id, strategy_id, mode, enabled, created_at,"
            " updated_at) VALUES (?, ?, 's1', 'notify', ?, ?, ?)",
            [sub, uid, enabled, NOW_ISO, NOW_ISO],
        )
    result = router.publish(_event(Audience.subscribers("s1"), category="signal"))
    rows = state.sql("SELECT user_id FROM notification_outbox")
    assert [r["user_id"] for r in rows] == [alice]
    assert len(result.notification_ids) == 1


def test_dedupe_key_blocks_a_repeat_forever_by_default(state, users, router, clock):
    alice = users["alice"].id
    add_device(state, alice, "psh_1")
    ev = _event(Audience.users(alice), dedupe_key="order:o1:filled")
    first = router.publish(ev)
    clock.now += timedelta(days=30)
    second = router.publish(ev)
    assert len(first.notification_ids) == 1
    assert second.notification_ids == () and second.deduped_user_ids == (alice,)
    assert len(_deliveries(state)) == 1
    assert state.sql("SELECT COUNT(*) FROM alerts")[0][0] == 1


def test_dedupe_is_per_user(state, users, router):
    alice, bob = users["alice"].id, users["bob"].id
    router.publish(_event(Audience.users(alice), dedupe_key="k"))
    result = router.publish(_event(Audience.users(alice, bob), dedupe_key="k"))
    assert result.deduped_user_ids == (alice,)
    assert len(result.notification_ids) == 1


def test_dedupe_window_rearms_the_key(state, users, clock, push):
    alice = users["alice"].id
    router = NotificationRouter(
        state, {"webpush": push}, OutboxSettings(dedupe_window_hours=6), clock=clock
    )
    ev = _event(Audience.users(alice), category="risk", dedupe_key="conn:broken")
    assert router.publish(ev).notification_ids
    clock.now += timedelta(hours=5, minutes=59)
    assert router.publish(ev).notification_ids == ()
    clock.now += timedelta(minutes=2)
    assert router.publish(ev).notification_ids
    rows = state.sql("SELECT dedupe_active FROM notification_outbox ORDER BY id")
    assert [r["dedupe_active"] for r in rows] == [0, 1]


def test_preferences_turn_channels_off_and_on(state, users, router, clock):
    alice = users["alice"].id
    add_device(state, alice, "psh_1")
    prefs = PreferenceStore(state)
    prefs.set(alice, [Preference("order", "webpush", False)], now=clock())
    prefs.set(alice, [Preference("order", "webhook", True)], now=clock())
    prefs.set_webhook(alice, "https://hooks.example/abc", now=clock())
    router.publish(_event(Audience.users(alice)))
    assert [d["channel"] for d in _deliveries(state)] == ["webhook"]


def test_price_and_event_alerts_switch_off_apart_from_signals(state, users, router, clock):
    alice = users["alice"].id
    add_device(state, alice, "psh_1")
    PreferenceStore(state).set(
        alice,
        [Preference("price_alert", "webpush", False), Preference("event_alert", "webpush", False)],
        now=clock(),
    )
    for category in ("price_alert", "event_alert", "signal"):
        router.publish(_event(Audience.users(alice), category=category, dedupe_key=category))
    [delivery] = _deliveries(state)
    [row] = state.sql(
        "SELECT category FROM notification_outbox WHERE id = ?", [delivery["notification_id"]]
    )
    assert row["category"] == "signal"
    # the feed still gets all three, and each key is deduped as before
    assert state.sql("SELECT COUNT(*) FROM alerts WHERE user_id = ?", [alice])[0][0] == 3
    assert router.publish(
        _event(Audience.users(alice), category="price_alert", dedupe_key="price_alert")
    ).deduped_user_ids == (alice,)


def test_strategy_specific_preference_wins(state, users, router, clock):
    alice = users["alice"].id
    add_device(state, alice, "psh_1")
    prefs = PreferenceStore(state)
    prefs.set(
        alice,
        [
            Preference("signal", "webpush", False),
            Preference("signal", "webpush", True, strategy_id="s_keep"),
        ],
        now=clock(),
    )
    router.publish(_event(Audience.users(alice), category="signal", strategy_id="s_other"))
    assert _deliveries(state) == []
    router.publish(_event(Audience.users(alice), category="signal", strategy_id="s_keep"))
    assert [d["channel"] for d in _deliveries(state)] == ["webpush"]


def test_high_urgency_falls_back_when_no_push_device(state, users, router, clock):
    alice = users["alice"].id  # has an email, no device
    router.publish(_event(Audience.users(alice), category="risk", title="Halted"))
    # webhook: the fake reaches everyone; email: Alice has an address.
    assert sorted(d["channel"] for d in _deliveries(state)) == ["email", "webhook"]


def test_no_fallback_when_a_push_device_exists(state, users, router):
    alice = users["alice"].id
    add_device(state, alice, "psh_1")
    router.publish(_event(Audience.users(alice), category="risk", title="Halted"))
    assert [d["channel"] for d in _deliveries(state)] == ["webpush"]


def test_no_fallback_for_normal_urgency(state, users, router):
    router.publish(_event(Audience.users(users["alice"].id)))
    assert _deliveries(state) == []


def test_explicit_opt_out_beats_the_fallback(state, users, router, clock):
    alice = users["alice"].id
    PreferenceStore(state).set(alice, [Preference("risk", "email", False)], now=clock())
    router.publish(_event(Audience.users(alice), category="risk", title="Halted"))
    assert [d["channel"] for d in _deliveries(state)] == ["webhook"]


def test_secrets_are_redacted_before_anything_is_stored(state, users, clock, push):
    alice = users["alice"].id
    router = NotificationRouter(
        state, {"webpush": push}, OutboxSettings(), clock=clock, secrets=lambda: ["sk-TOPSECRET"]
    )
    router.publish(
        _event(
            Audience.users(alice),
            title="broker error sk-TOPSECRET",
            body="GET https://x.example/?api_key=abc123 failed",
        )
    )
    [row] = state.sql("SELECT title, body FROM notification_outbox")
    [alert] = state.sql("SELECT title, message FROM alerts")
    for text in (*row, *alert):
        assert "sk-TOPSECRET" not in text and "abc123" not in text


def test_publish_joins_the_callers_transaction(state, users, router):
    alice = users["alice"].id
    with pytest.raises(RuntimeError), state.transaction():
        router.publish(_event(Audience.users(alice)))
        raise RuntimeError("the producer failed")
    assert state.sql("SELECT COUNT(*) FROM notification_outbox")[0][0] == 0
    assert state.sql("SELECT COUNT(*) FROM alerts")[0][0] == 0


def _strategy_with_subscriber(state, user_id, status="active"):
    state.execute(
        "INSERT INTO strategies (id, class_path, params_json, status, created_at, updated_at)"
        " VALUES ('mom', 'm:C', '{}', 'shadow', ?, ?)",
        [NOW_ISO, NOW_ISO],
    )
    if status != "shadow":
        state.execute(
            "INSERT INTO status_changes (strategy_id, from_status, to_status, actor, reason,"
            " override, created_at) VALUES ('mom', 'shadow', ?, 't', 'seed', 1, ?)",
            [status, NOW_ISO],
        )
        state.execute("UPDATE strategies SET status = ? WHERE id = 'mom'", [status])
    state.execute(
        "INSERT INTO subscriptions (id, user_id, strategy_id, mode, created_at, updated_at)"
        " VALUES ('sub_1', ?, 'mom', 'notify', ?, ?)",
        [user_id, NOW_ISO, NOW_ISO],
    )


def test_notify_signal_is_minimal_and_idempotent_per_as_of(state, users, router):
    alice = users["alice"].id
    add_device(state, alice, "psh_1")
    _strategy_with_subscriber(state, alice)
    kw = {"strategy_id": "mom", "ticker": "AAPL.US", "kind": "entry", "as_of": "2026-01-05"}
    first = notify_signal(router, **kw)
    again = notify_signal(router, **kw)  # a re-run tick
    assert len(first.notification_ids) == 1 and again.notification_ids == ()
    [row] = state.sql("SELECT * FROM notification_outbox")
    assert row["category"] == "signal" and row["strategy_id"] == "mom"
    assert row["dedupe_key"] == "signal:mom:AAPL.US:entry:2026-01-05"
    assert row["title"] == "AAPL.US: entry signal"
    assert row["deep_link"].startswith("/signals?")
    next_day = notify_signal(router, **{**kw, "as_of": "2026-01-06"})
    assert len(next_day.notification_ids) == 1


def test_notify_signal_labels_strategies_on_trial(state, users, router):
    _strategy_with_subscriber(state, users["alice"].id, status="shadow")
    notify_signal(router, strategy_id="mom", ticker="MSFT.US", kind="exit", as_of="2026-01-05")
    [row] = state.sql("SELECT body FROM notification_outbox")
    assert "(on trial)" in row["body"] and "incubating" not in row["body"]


def test_notify_signals_batches_in_one_transaction(state, users, router):
    _strategy_with_subscriber(state, users["alice"].id)
    notices = [
        SignalNotice("mom", "AAPL.US", "entry", "2026-01-05"),
        SignalNotice("mom", "MSFT.US", "exit", "2026-01-05"),
    ]
    results = notify_signals(router, notices)
    assert [len(r.notification_ids) for r in results] == [1, 1]


def test_notify_signal_rejects_unknown_kind(router):
    with pytest.raises(ValueError):
        notify_signal(router, strategy_id="mom", ticker="A", kind="yolo", as_of="2026-01-05")
