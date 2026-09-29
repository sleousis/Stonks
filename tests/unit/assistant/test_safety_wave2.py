"""Review wave 2, item 5: a prompt injection in tool data must not silently
mark every alert read (halt alerts included), and deleting a conversation
must not reset the assistant's write rate limit."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from stonks.assistant import catalog, guard
from stonks.assistant.settings import AssistantEnvelope
from stonks.assistant.store import ConversationStore
from stonks.store.state import SqliteState

OWNER = "usr_owner"


@pytest.fixture
def path(tmp_path):
    p = tmp_path / "state.sqlite"
    with SqliteState(p) as state:
        state.migrate()
    return p


@pytest.fixture
def store(path) -> ConversationStore:
    return ConversationStore(lambda: SqliteState(path))


def test_marking_notifications_read_asks_first():
    assert not catalog.runs_without_asking("mark_notifications_read", {})
    assert not catalog.runs_without_asking("mark_notifications_read", {"ids": [1]})
    assert "mark_notifications_read" not in catalog.RESEARCH_WRITES


def test_deleting_a_conversation_keeps_its_writes_in_the_rate_limit(store, path):
    env = AssistantEnvelope(max_writes_per_minute=2)
    for _ in range(2):
        conv = store.create(OWNER)
        action, refusal = store.reserve_action(conv.id, "t1", "create_draft", {"name": "x"})
        assert action is not None and refusal is None
        store.delete(OWNER, conv.id)
    now = datetime.now(UTC)
    with SqliteState(path) as state:
        assert guard.writes_since(state, OWNER, now - timedelta(minutes=1)) == 2
        burst = guard.over_rate(state, OWNER, env, now)
    assert burst is not None and "a minute" in burst
