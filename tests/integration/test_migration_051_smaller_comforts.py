"""Migration 051 (roadmap 23.17): the ``screen_alert`` notification
category, screen alert tables, the demo portfolio seed and CSV statement
imports. The notification tables are rebuilt to widen their category
checks, and their rows, ids and indexes survive."""

from __future__ import annotations

import sqlite3

import pytest

from stonks.store.state import SqliteState

NOW = "2026-09-28T06:00:00+00:00"


@pytest.fixture
def state(tmp_path):
    s = SqliteState(tmp_path / "state.sqlite")
    s.migrate()
    yield s
    s.close()


def test_every_notification_table_takes_screen_alert(state):
    alert_id = state.execute(
        "INSERT INTO alerts (level, title, message, created_at, user_id, category)"
        " VALUES ('info', 't', 'm', ?, 'usr_owner', 'screen_alert')",
        [NOW],
    ).lastrowid
    state.execute(
        "INSERT INTO notification_outbox (user_id, category, level, urgency, title, body,"
        " dedupe_key, alert_id, created_at)"
        " VALUES ('usr_owner', 'screen_alert', 'info', 'normal', 't', 'b', 'k', ?, ?)",
        [alert_id, NOW],
    )
    state.execute(
        "INSERT INTO notification_prefs (user_id, category, channel, enabled, updated_at)"
        " VALUES ('usr_owner', 'screen_alert', 'webpush', 0, ?)",
        [NOW],
    )
    with pytest.raises(sqlite3.IntegrityError):
        state.execute(
            "INSERT INTO notification_prefs (user_id, category, channel, enabled, updated_at)"
            " VALUES ('usr_owner', 'gossip', 'webpush', 0, ?)",
            [NOW],
        )


def test_indexes_survive_the_rebuild(state):
    names = {
        r["name"]
        for r in state.sql(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"
        )
    }
    assert {
        "idx_alerts_level",
        "idx_alerts_user",
        "ux_outbox_user_dedupe",
        "idx_outbox_user",
        "idx_deliveries_due",
        "idx_deliveries_user",
        "ux_notification_prefs",
        "idx_broker_activities_import",
    } <= names


def test_new_tables_exist(state):
    tables = {r["name"] for r in state.sql("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {
        "screen_alerts",
        "screen_alert_matches",
        "screen_alert_events",
        "demo_portfolios",
        "statement_imports",
    } <= tables
    cols = {r["name"] for r in state.sql("PRAGMA table_info(broker_activities)")}
    assert "import_id" in cols


def test_weekly_alerts_need_a_weekday(state):
    state.execute(
        "INSERT INTO screens (id, owner_id, name, spec_json, created_at, updated_at)"
        " VALUES ('scr_1', 'usr_owner', 'cheap', '{}', ?, ?)",
        [NOW, NOW],
    )
    with pytest.raises(sqlite3.IntegrityError):
        state.execute(
            "INSERT INTO screen_alerts (screen_id, owner_id, cadence, created_at, updated_at)"
            " VALUES ('scr_1', 'usr_owner', 'weekly', ?, ?)",
            [NOW, NOW],
        )
