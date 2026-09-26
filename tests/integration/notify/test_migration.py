"""The notifications migration: outbox, deliveries, push subscriptions,
preferences and per-user settings, with their integrity rules."""

from __future__ import annotations

import sqlite3

import pytest

NOW = "2026-01-05T15:00:00+00:00"


def test_tables_exist(state):
    tables = set(state.tables())
    assert {
        "notification_outbox",
        "notification_deliveries",
        "push_subscriptions",
        "notification_prefs",
        "notification_settings",
    } <= tables


def _outbox(state, user_id, key, active=1):
    return state.execute(
        "INSERT INTO notification_outbox (user_id, category, level, urgency, title, body,"
        " dedupe_key, dedupe_active, created_at) VALUES (?, 'signal', 'info', 'normal',"
        " 't', 'b', ?, ?, ?)",
        [user_id, key, active, NOW],
    ).lastrowid


def test_active_dedupe_key_is_unique_per_user(state, users):
    a, b = users["alice"].id, users["bob"].id
    _outbox(state, a, "k")
    _outbox(state, b, "k")  # other user: fine
    _outbox(state, a, None)
    _outbox(state, a, None)  # no key: never deduped
    with pytest.raises(sqlite3.IntegrityError):
        _outbox(state, a, "k")
    state.execute("UPDATE notification_outbox SET dedupe_active = 0 WHERE user_id = ?", [a])
    _outbox(state, a, "k")  # expired key re-arms


def test_outbox_rejects_unknown_category_and_urgency(state, users):
    with pytest.raises(sqlite3.IntegrityError):
        state.execute(
            "INSERT INTO notification_outbox (user_id, category, level, urgency, title, body,"
            " created_at) VALUES (?, 'gossip', 'info', 'normal', 't', 'b', ?)",
            [users["alice"].id, NOW],
        )
    with pytest.raises(sqlite3.IntegrityError):
        state.execute(
            "INSERT INTO notification_outbox (user_id, category, level, urgency, title, body,"
            " created_at) VALUES (?, 'risk', 'info', 'screaming', 't', 'b', ?)",
            [users["alice"].id, NOW],
        )


def test_one_delivery_per_notification_channel_and_target(state, users):
    nid = _outbox(state, users["alice"].id, None)
    sql = (
        "INSERT INTO notification_deliveries (notification_id, user_id, channel, target_id,"
        " status, next_attempt_at, created_at, updated_at)"
        " VALUES (?, ?, 'webpush', ?, 'pending', ?, ?, ?)"
    )
    state.execute(sql, [nid, users["alice"].id, "psh_1", NOW, NOW, NOW])
    state.execute(sql, [nid, users["alice"].id, "psh_2", NOW, NOW, NOW])
    with pytest.raises(sqlite3.IntegrityError):
        state.execute(sql, [nid, users["alice"].id, "psh_1", NOW, NOW, NOW])


def test_push_endpoint_is_unique(state, users):
    sql = (
        "INSERT INTO push_subscriptions (id, user_id, endpoint, p256dh, auth, created_at)"
        " VALUES (?, ?, 'https://push.example/abc', 'k', 'a', ?)"
    )
    state.execute(sql, ["psh_1", users["alice"].id, NOW])
    with pytest.raises(sqlite3.IntegrityError):
        state.execute(sql, ["psh_2", users["bob"].id, NOW])


def test_quiet_hours_format_is_checked(state, users):
    with pytest.raises(sqlite3.IntegrityError):
        state.execute(
            "INSERT INTO notification_settings (user_id, quiet_start, quiet_end, updated_at)"
            " VALUES (?, '25:00', '07:00', ?)",
            [users["alice"].id, NOW],
        )
    state.execute(
        "INSERT INTO notification_settings (user_id, quiet_start, quiet_end, updated_at)"
        " VALUES (?, '22:00', '07:00', ?)",
        [users["alice"].id, NOW],
    )
