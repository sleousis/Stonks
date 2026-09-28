"""Migration 035: the ``price_alert`` and ``event_alert`` notification
categories and per-kind event alert switches. ``alerts``,
``notification_outbox``, ``notification_deliveries`` and
``notification_prefs`` are rebuilt to widen their category checks. Their
rows, ids, indexes and the links between them survive."""

from __future__ import annotations

import shutil
import sqlite3

import pytest

import stonks.store.state as state_module
from stonks.store.state import SqliteState

NOW = "2026-09-27T06:00:00+00:00"


@pytest.fixture
def state(tmp_path):
    s = SqliteState(tmp_path / "state.sqlite")
    s.migrate()
    yield s
    s.close()


def _alert(state: SqliteState, category: str) -> int:
    return state.execute(
        "INSERT INTO alerts (level, title, message, created_at, user_id, category)"
        " VALUES ('info', 't', 'm', ?, 'usr_owner', ?)",
        [NOW, category],
    ).lastrowid


def _outbox(state: SqliteState, category: str, alert_id: int | None, key: str) -> int:
    return state.execute(
        "INSERT INTO notification_outbox (user_id, category, level, urgency, title, body,"
        " dedupe_key, alert_id, created_at)"
        " VALUES ('usr_owner', ?, 'info', 'normal', 't', 'b', ?, ?, ?)",
        [category, key, alert_id, NOW],
    ).lastrowid


def _pref(state: SqliteState, category: str, channel: str = "webpush") -> None:
    state.execute(
        "INSERT INTO notification_prefs (user_id, category, channel, enabled, updated_at)"
        " VALUES ('usr_owner', ?, ?, 0, ?)",
        [category, channel, NOW],
    )


@pytest.mark.parametrize("category", ["price_alert", "event_alert"])
def test_every_table_takes_the_new_categories(state, category):
    alert_id = _alert(state, category)
    _outbox(state, category, alert_id, f"k:{category}")
    _pref(state, category)


def test_unknown_categories_are_still_refused(state):
    with pytest.raises(sqlite3.IntegrityError):
        _alert(state, "gossip")
    with pytest.raises(sqlite3.IntegrityError):
        _outbox(state, "gossip", None, "k")
    with pytest.raises(sqlite3.IntegrityError):
        _pref(state, "gossip")


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
    } <= names
    _outbox(state, "signal", None, "same")
    with pytest.raises(sqlite3.IntegrityError):
        _outbox(state, "signal", None, "same")  # the dedupe index still holds


def test_event_alert_prefs_table(state):
    state.execute(
        "INSERT INTO event_alert_prefs (user_id, topic, enabled, updated_at)"
        " VALUES ('usr_owner', 'earnings', 0, ?)",
        [NOW],
    )
    with pytest.raises(sqlite3.IntegrityError):
        state.execute(
            "INSERT INTO event_alert_prefs (user_id, topic, enabled, updated_at)"
            " VALUES ('usr_owner', 'earnings', 1, ?)",
            [NOW],
        )


def test_rows_ids_and_links_survive(tmp_path, monkeypatch):
    old = tmp_path / "old_migrations"
    old.mkdir()
    for path in sorted(state_module.MIGRATIONS_DIR.glob("*.sql")):
        if int(path.stem.split("_", 1)[0]) < 35:
            shutil.copy(path, old / path.name)
    monkeypatch.setattr(state_module, "MIGRATIONS_DIR", old)
    s = SqliteState(tmp_path / "state.sqlite")
    try:
        s.migrate()
        # A deleted alert leaves a gap: AUTOINCREMENT must not hand its id out again.
        gone = _alert(s, "system")
        alert_id = _alert(s, "signal")
        s.execute("DELETE FROM alerts WHERE id = ?", [gone])
        top = _alert(s, "order")
        s.execute("DELETE FROM alerts WHERE id = ?", [top])
        s.execute("UPDATE alerts SET read_at = ?, dedupe_key = 'd' WHERE id = ?", [NOW, alert_id])
        nid = _outbox(s, "signal", alert_id, "sig:1")
        s.execute(
            "INSERT INTO notification_deliveries (notification_id, user_id, channel, target_id,"
            " status, attempts, next_attempt_at, created_at, updated_at)"
            " VALUES (?, 'usr_owner', 'webpush', 'psh_1', 'failed', 2, ?, ?, ?)",
            [nid, NOW, NOW, NOW],
        )
        _pref(s, "signal")
        monkeypatch.undo()
        s.migrate()

        assert 34 in s.applied_migrations()
        alert = s.sql("SELECT * FROM alerts WHERE id = ?", [alert_id])[0]
        assert (alert["category"], alert["read_at"], alert["dedupe_key"]) == ("signal", NOW, "d")
        outbox = s.sql("SELECT * FROM notification_outbox WHERE id = ?", [nid])[0]
        assert (outbox["alert_id"], outbox["dedupe_key"]) == (alert_id, "sig:1")
        delivery = s.sql("SELECT * FROM notification_deliveries")[0]
        assert (delivery["notification_id"], delivery["status"], delivery["attempts"]) == (
            nid,
            "failed",
            2,
        )
        pref = s.sql("SELECT * FROM notification_prefs")[0]
        assert (pref["category"], pref["enabled"]) == ("signal", 0)
        assert _alert(s, "event_alert") > top
        # Deleting an outbox row still cascades to its deliveries.
        assert s.sql("PRAGMA foreign_key_check") == []
        s.execute("DELETE FROM notification_outbox WHERE id = ?", [nid])
        assert s.sql("SELECT COUNT(*) AS n FROM notification_deliveries")[0]["n"] == 0
    finally:
        s.close()
