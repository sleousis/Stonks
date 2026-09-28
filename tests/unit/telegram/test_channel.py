"""The ``telegram`` notification channel."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from stonks.accounts import Role, UserRepository
from stonks.notify.channels import build_channels, channel_defaults
from stonks.notify.events import Message
from stonks.notify.settings import NotifySettings
from stonks.store.state import SqliteState
from stonks.telegram.api import TelegramApiError
from stonks.telegram.channel import TelegramChannel, format_message
from stonks.telegram.fake import FakeTelegramApi
from stonks.telegram.links import LinkStore

NOW = datetime(2026, 9, 27, tzinfo=UTC)


def _message(user_id: str = "u") -> Message:
    return Message(
        notification_id=1,
        user_id=user_id,
        category="order",
        level="info",
        urgency="normal",
        title="AAPL.US filled",
        body="bought 10",
        deep_link="/orders",
        dedupe_key=None,
        ttl_seconds=60,
    )


@pytest.fixture
def state(tmp_path):
    with SqliteState(tmp_path / "s.sqlite") as s:
        s.migrate()
        yield s


def _linked_user(state) -> str:
    uid = (
        UserRepository(state)
        .create(display_name="a", role=Role.TRADER, actor="service:test", email="a@example.com")
        .id
    )
    store = LinkStore(state)
    store.redeem(store.create_code(uid, minutes=5, actor="t")[0], "77", "alice")
    return uid


def test_registered_on_by_default_and_a_fallback():
    assert channel_defaults()["telegram"] == (True, True)


def test_built_only_with_the_env_token(monkeypatch):
    monkeypatch.delenv("STONKS_TELEGRAM_BOT_TOKEN", raising=False)
    assert "telegram" not in build_channels(NotifySettings())
    monkeypatch.setenv("STONKS_TELEGRAM_BOT_TOKEN", "1:abc")
    assert isinstance(build_channels(NotifySettings())["telegram"], TelegramChannel)


def test_targets_the_linked_chat_and_sends(state):
    uid = _linked_user(state)
    api = FakeTelegramApi()
    channel = TelegramChannel(api, base_url="https://stonks.example")
    assert channel.targets(state, uid) == ["77"]
    assert channel.targets(state, "usr_other") == []
    target = channel.resolve(state, uid, "77")
    assert channel.send(_message(uid), target).outcome == "sent"
    assert api.sent == [("77", "AAPL.US filled\nbought 10\nOpen: https://stonks.example/orders")]
    assert channel.resolve(state, uid, "78") is None


def test_blocked_bot_is_gone_and_unlinks(state):
    uid = _linked_user(state)
    api = FakeTelegramApi(send_error=TelegramApiError("blocked", status=403, retryable=False))
    channel = TelegramChannel(api)
    result = channel.send(_message(uid), "77")
    assert result.outcome == "gone"
    channel.on_failed(state, uid, "77", result, NOW)
    assert LinkStore(state).for_user(uid) is None


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (TelegramApiError("slow", status=429, retryable=True, retry_after=2), "retry"),
        (TelegramApiError("down", status=None, retryable=True), "retry"),
        (TelegramApiError("odd", status=401, retryable=False), "dead"),
        # a bad request is not a lost chat: the link stays (review 2026-09-27)
        (TelegramApiError("Bad Request: message is too long", status=400, retryable=False), "dead"),
        (TelegramApiError("Bad Request: chat not found", status=400, retryable=False), "gone"),
    ],
)
def test_error_outcomes(error, outcome):
    assert TelegramChannel(FakeTelegramApi(send_error=error)).send(_message(), "1").outcome == (
        outcome
    )


def test_format_without_base_url_keeps_the_path():
    assert format_message(_message()).endswith("Open: /orders")
