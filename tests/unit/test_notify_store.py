"""StoreNotifier: every notification lands in the ``alerts`` table, redacted."""

from __future__ import annotations

import json

import pytest

from stonks.config import NotifyConfig
from stonks.notify import (
    CompositeNotifier,
    LogNotifier,
    Notification,
    StoreNotifier,
    build_notifier,
)
from stonks.store.state import SqliteState

SECRET = "sk-live-SUPERSECRET"


@pytest.fixture
def state_path(tmp_path):
    path = tmp_path / "state.sqlite"
    with SqliteState(path) as state:
        state.migrate()
    return path


def _rows(path) -> list[dict]:
    with SqliteState(path) as state:
        return [dict(r) for r in state.sql("SELECT * FROM alerts ORDER BY id")]


def test_persists_level_title_message_context_and_timestamp(state_path):
    StoreNotifier(state_path).notify(
        Notification(
            level="warning",
            title="orders rejected",
            message="2 order(s) rejected",
            fields={"tick_id": "tick_1", "rejected": ["A.US", "B.US"]},
        )
    )
    [row] = _rows(state_path)
    assert row["level"] == "warning"
    assert row["title"] == "orders rejected"
    assert row["message"] == "2 order(s) rejected"
    assert json.loads(row["context_json"]) == {"tick_id": "tick_1", "rejected": ["A.US", "B.US"]}
    assert row["created_at"].startswith("20")


def test_configured_secrets_are_redacted_everywhere(state_path):
    StoreNotifier(state_path, secrets=lambda: [SECRET]).notify(
        Notification(
            level="error",
            title=f"tick failed {SECRET}",
            message=f"HTTPError for url: https://x.test/?api_token={SECRET}",
            fields={"detail": f"key was {SECRET}", "nested": {"deep": [SECRET]}},
        )
    )
    [row] = _rows(state_path)
    blob = json.dumps(row)
    assert SECRET not in blob
    assert "***" in row["title"] and "***" in row["message"]


def test_secret_looking_keys_and_credential_params_are_redacted(state_path):
    StoreNotifier(state_path).notify(
        Notification(
            level="error",
            title="t",
            message="GET https://api.test/v1?api_token=abc123def&x=1 failed; Bearer tok.en-99",
            fields={"api_key": "abc", "Authorization": "Bearer zzz", "webhook_url": "https://h/x"},
        )
    )
    [row] = _rows(state_path)
    ctx = json.loads(row["context_json"])
    assert ctx == {"api_key": "***", "Authorization": "***", "webhook_url": "***"}
    assert "abc123def" not in row["message"]
    assert "tok.en-99" not in row["message"]


def test_store_failure_never_raises(tmp_path):
    # No migrations applied: the insert fails, notify() swallows it.
    StoreNotifier(tmp_path / "missing" / "state.sqlite").notify(
        Notification(level="error", title="t", message="m")
    )


def test_store_receives_every_level_even_below_min_level(state_path):
    notifier = build_notifier(
        NotifyConfig(backends=["log", "store"], min_level="error"), state_path=state_path
    )
    notifier.notify(Notification(level="info", title="tick ok", message="fine"))
    assert [r["level"] for r in _rows(state_path)] == ["info"]


def test_build_notifier_wires_store_backend(state_path):
    notifier = build_notifier(NotifyConfig(backends=["log", "store"]), state_path=state_path)
    assert isinstance(notifier, CompositeNotifier)
    kinds = [type(c) for c in notifier.children]
    assert kinds == [LogNotifier, StoreNotifier]


def test_build_notifier_skips_store_without_state_path():
    notifier = build_notifier(NotifyConfig(backends=["store"]))
    assert notifier.children == []


def test_store_is_a_default_backend():
    assert NotifyConfig().backends == ["log", "store"]
