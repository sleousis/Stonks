"""Link codes and chat links (roadmap 20.3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from stonks.accounts import Role, UserRepository
from stonks.store.state import SqliteState
from stonks.telegram.links import ALPHABET, CODE_LENGTH, LinkError, LinkStore, hash_code

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


@pytest.fixture
def state(tmp_path):
    with SqliteState(tmp_path / "s.sqlite") as s:
        s.migrate()
        yield s


def _user(state, email: str) -> str:
    return (
        UserRepository(state)
        .create(display_name=email, role=Role.TRADER, actor="service:test", email=email)
        .id
    )


def test_code_is_stored_hashed_and_works_once(state):
    uid = _user(state, "a@example.com")
    store = LinkStore(state)
    code, expires = store.create_code(uid, minutes=10, actor="user:x", now=NOW)
    assert len(code) == CODE_LENGTH and set(code) <= set(ALPHABET)
    assert expires == NOW + timedelta(minutes=10)
    rows = state.sql("SELECT code_hash FROM telegram_link_codes")
    assert rows[0]["code_hash"] == hash_code(code) and code not in rows[0]["code_hash"]
    link = store.redeem(code.lower(), "42", "alice", now=NOW)
    assert (link.chat_id, link.user_id, link.username) == ("42", uid, "alice")
    with pytest.raises(LinkError):
        store.redeem(code, "43", None, now=NOW)
    actions = [r["action"] for r in state.sql("SELECT action FROM audit_log")]
    assert "telegram.link_code" in actions and "telegram.link" in actions


def test_expired_and_unknown_codes_are_refused(state):
    uid = _user(state, "a@example.com")
    store = LinkStore(state)
    code, _ = store.create_code(uid, minutes=10, actor="user:x", now=NOW)
    with pytest.raises(LinkError):
        store.redeem(code, "42", None, now=NOW + timedelta(minutes=11))
    with pytest.raises(LinkError):
        store.redeem("ZZZZZZZZ", "42", None, now=NOW)
    assert store.for_chat("42") is None


def test_new_code_retires_older_ones(state):
    uid = _user(state, "a@example.com")
    store = LinkStore(state)
    old, _ = store.create_code(uid, minutes=10, actor="user:x", now=NOW)
    new, _ = store.create_code(uid, minutes=10, actor="user:x", now=NOW)
    with pytest.raises(LinkError):
        store.redeem(old, "42", None, now=NOW)
    assert store.redeem(new, "42", None, now=NOW).user_id == uid


def test_one_chat_one_user(state):
    alice, bob = _user(state, "a@example.com"), _user(state, "b@example.com")
    store = LinkStore(state)
    store.redeem(store.create_code(alice, minutes=5, actor="t", now=NOW)[0], "1", None, now=NOW)
    code, _ = store.create_code(bob, minutes=5, actor="t", now=NOW)
    with pytest.raises(LinkError, match="another account"):
        store.redeem(code, "1", None, now=NOW)
    assert store.for_chat("1").user_id == alice
    # the refused code is still usable from another chat
    assert store.redeem(code, "2", None, now=NOW).user_id == bob


def test_relinking_a_user_moves_them_to_the_new_chat(state):
    alice = _user(state, "a@example.com")
    store = LinkStore(state)
    store.redeem(store.create_code(alice, minutes=5, actor="t", now=NOW)[0], "1", None, now=NOW)
    store.redeem(store.create_code(alice, minutes=5, actor="t", now=NOW)[0], "2", None, now=NOW)
    assert store.for_chat("1") is None and store.for_user(alice).chat_id == "2"


def test_unlink(state):
    alice = _user(state, "a@example.com")
    store = LinkStore(state)
    store.redeem(store.create_code(alice, minutes=5, actor="t", now=NOW)[0], "1", None, now=NOW)
    assert store.unlink_chat("1", actor="user:x") is True
    assert store.unlink_user(alice, actor="user:x") is False
    assert store.for_user(alice) is None
    assert "telegram.unlink" in [r["action"] for r in state.sql("SELECT action FROM audit_log")]


def test_offset_round_trip(state):
    store = LinkStore(state)
    assert store.get_offset() is None
    store.set_offset(7)
    store.set_offset(9)
    assert store.get_offset() == 9
