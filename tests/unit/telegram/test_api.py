"""The Bot API client: parsing, error mapping, token redaction."""

from __future__ import annotations

from typing import Any

import pytest
import requests

from stonks.telegram.api import MAX_TEXT, HttpTelegramApi, TelegramApiError, parse_update
from stonks.telegram.fake import FakeTelegramApi
from stonks.telegram.settings import bot_configured, bot_token

TOKEN = "123456:SECRET-token-value"


class _Resp:
    def __init__(self, status: int, payload: Any) -> None:
        self.status_code = status
        self._payload = payload

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _Session:
    def __init__(self, resp: _Resp | Exception) -> None:
        self.resp = resp
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, json: dict, timeout: float) -> _Resp:
        self.calls.append((url, json))
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


def _message(update_id: int, text: str = "/help", chat: int = 5) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": chat, "type": "private"},
            "from": {"username": "alice"},
            "text": text,
        },
    }


def test_get_updates_parses_text_messages_and_skips_others():
    session = _Session(
        _Resp(200, {"ok": True, "result": [_message(3), {"update_id": 4, "poll": {}}]})
    )
    api = HttpTelegramApi(TOKEN, session=session)
    updates = api.get_updates(3, 0)
    assert [(u.update_id, u.chat_id, u.text, u.username) for u in updates] == [
        (3, "5", "/help", "alice")
    ]
    assert session.calls[0][1]["offset"] == 3


def test_send_message_clips_long_text():
    session = _Session(_Resp(200, {"ok": True, "result": {}}))
    HttpTelegramApi(TOKEN, session=session).send_message("5", "x" * (MAX_TEXT + 50))
    assert len(session.calls[0][1]["text"]) == MAX_TEXT


@pytest.mark.parametrize(
    ("status", "retryable"), [(403, False), (400, False), (429, True), (502, True)]
)
def test_errors_map_status_and_never_show_the_token(status, retryable):
    payload = {"ok": False, "error_code": status, "description": f"bad {TOKEN}"}
    if status == 429:
        payload["parameters"] = {"retry_after": 3}
    api = HttpTelegramApi(TOKEN, session=_Session(_Resp(status, payload)))
    with pytest.raises(TelegramApiError) as err:
        api.send_message("5", "hi")
    assert err.value.status == status and err.value.retryable is retryable
    assert TOKEN not in str(err.value)
    if status == 429:
        assert err.value.retry_after == 3


def test_network_errors_are_retryable_and_redacted():
    boom = requests.ConnectionError(f"https://api.telegram.org/bot{TOKEN}/sendMessage refused")
    api = HttpTelegramApi(TOKEN, session=_Session(boom))
    with pytest.raises(TelegramApiError) as err:
        api.send_message("5", "hi")
    assert err.value.retryable and TOKEN not in str(err.value)
    assert TOKEN not in repr(api)


def test_parse_update_rejects_junk():
    assert parse_update({"update_id": 1}) is None
    assert parse_update("x") is None
    assert parse_update({"update_id": 1, "message": {"chat": {"id": 1}}}) is None


def test_token_comes_from_env_only():
    assert bot_token({}) is None and not bot_configured({})
    assert bot_token({"STONKS_TELEGRAM_BOT_TOKEN": " t "}) == "t"
    with pytest.raises(ValueError):
        HttpTelegramApi("")


def test_fake_honours_offsets():
    api = FakeTelegramApi()
    first = api.push(1, "a")
    api.push(1, "b")
    assert len(api.get_updates(None, 0)) == 2
    assert [u.text for u in api.get_updates(first.update_id + 1, 0)] == ["b"]
