"""The :class:`TelegramApi` seam and its HTTP implementation.

Only this module talks HTTP to Telegram. The rest of the package sees our
own :class:`TelegramUpdate` and :class:`TelegramApiError`, never a raw Bot
API payload. The token is part of every Bot API URL, so every error text
is scrubbed of it before it leaves this module.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import requests

#: Telegram's limit on one message's text.
MAX_TEXT = 4096


@dataclass(frozen=True)
class TelegramUpdate:
    """One incoming text message (other update kinds are skipped)."""

    update_id: int
    chat_id: str
    text: str
    username: str | None = None
    #: ``private``, ``group``, ``supergroup`` or ``channel``.
    chat_type: str = "private"


class TelegramApiError(RuntimeError):
    """A Bot API call failed. ``status`` is the HTTP status (or the Bot
    API's ``error_code``), ``None`` for a network failure. ``retryable``
    says whether trying again later may work."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = True,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after


class TelegramApi(ABC):
    @abstractmethod
    def get_updates(self, offset: int | None, timeout: int) -> list[TelegramUpdate]:
        """Updates with an id >= ``offset``, waiting up to ``timeout``
        seconds for one. Passing a later offset confirms the earlier ones."""

    @abstractmethod
    def send_message(self, chat_id: str, text: str) -> None:
        """Send plain text (no markup) to ``chat_id``."""


def clip(text: str) -> str:
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


def parse_update(raw: Any) -> TelegramUpdate | None:
    """Our update for a Bot API update dict, or ``None`` for anything that
    is not a text message."""
    if not isinstance(raw, dict) or not isinstance(raw.get("update_id"), int):
        return None
    message = raw.get("message") or raw.get("edited_message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat")
    text = message.get("text")
    if not isinstance(chat, dict) or chat.get("id") is None or not isinstance(text, str):
        return None
    sender = message.get("from")
    username = (sender.get("username") if isinstance(sender, dict) else None) or chat.get(
        "username"
    )
    return TelegramUpdate(
        update_id=raw["update_id"],
        chat_id=str(chat["id"]),
        text=text,
        username=str(username) if username else None,
        chat_type=str(chat.get("type") or "private"),
    )


class HttpTelegramApi(TelegramApi):
    """The Bot API over HTTPS (``requests``)."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://api.telegram.org",
        timeout_seconds: float = 10.0,
        session: Any | None = None,
    ) -> None:
        if not token:
            raise ValueError("the Telegram bot needs STONKS_TELEGRAM_BOT_TOKEN")
        self._token = token
        self._base = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._session = session or requests.Session()

    def __repr__(self) -> str:
        return f"HttpTelegramApi(base_url={self._base!r}, token='***')"

    def redact(self, text: str) -> str:
        return text.replace(self._token, "***")

    def get_updates(self, offset: int | None, timeout: int) -> list[TelegramUpdate]:
        body: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            body["offset"] = offset
        result = self._call("getUpdates", body, wait=timeout)
        updates = [parse_update(u) for u in (result if isinstance(result, list) else [])]
        return [u for u in updates if u is not None]

    def send_message(self, chat_id: str, text: str) -> None:
        self._call(
            "sendMessage",
            {"chat_id": chat_id, "text": clip(text), "disable_web_page_preview": True},
        )

    def _call(self, method: str, body: dict[str, Any], *, wait: float = 0.0) -> Any:
        url = f"{self._base}/bot{self._token}/{method}"
        try:
            resp = self._session.post(url, json=body, timeout=self._timeout + wait)
        except requests.RequestException as exc:
            raise TelegramApiError(
                self.redact(f"{method}: {type(exc).__name__}: {exc}"), retryable=True
            ) from None
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        if resp.status_code < 400 and isinstance(payload, dict) and payload.get("ok"):
            return payload.get("result")
        status = payload.get("error_code") if isinstance(payload, dict) else None
        status = status if isinstance(status, int) else resp.status_code
        description = payload.get("description", "") if isinstance(payload, dict) else ""
        params = payload.get("parameters") if isinstance(payload, dict) else None
        retry_after = params.get("retry_after") if isinstance(params, dict) else None
        raise TelegramApiError(
            self.redact(f"{method}: HTTP {status}: {description}"[:300]),
            status=status,
            retryable=status == 429 or status >= 500,
            retry_after=float(retry_after) if isinstance(retry_after, int | float) else None,
        )
