"""The ``telegram`` notification channel.

A user's target is their linked chat. The channel runs whenever the bot
token is set (sending needs no polling). It is on by default for linked
users and a fallback for ``high`` urgency, like email and the webhook.
Payloads stay minimal: the title, one line and the deep link as text.

A send that Telegram refuses with 403 (the person blocked the bot or left
the chat) is ``gone``: the link is removed. 429 and server errors retry.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from stonks.logging import get_logger
from stonks.notify.channels import Channel, DeliveryResult, register_channel
from stonks.notify.events import Message
from stonks.notify.settings import NotifySettings
from stonks.store.state import SqliteState
from stonks.telegram.api import HttpTelegramApi, TelegramApi, TelegramApiError
from stonks.telegram.links import LinkStore
from stonks.telegram.settings import bot_token

_log = get_logger("stonks.telegram.channel")


def format_message(message: Message, base_url: str | None = None) -> str:
    lines = [message.title]
    if message.body:
        lines.append(message.body)
    if message.deep_link:
        lines.append(f"Open: {(base_url or '').rstrip('/')}{message.deep_link}")
    return "\n".join(lines)


@register_channel("telegram")
class TelegramChannel(Channel):
    default_enabled = True
    fallback = True

    def __init__(self, api: TelegramApi, *, base_url: str | None = None) -> None:
        self._api = api
        self._base_url = base_url

    def __repr__(self) -> str:
        return "TelegramChannel()"

    @classmethod
    def from_settings(cls, settings: NotifySettings) -> TelegramChannel | None:
        token = bot_token()
        if token is None:
            return None
        return cls(HttpTelegramApi(token), base_url=settings.outbox.public_base_url)

    def targets(self, state: SqliteState, user_id: str) -> list[str]:
        link = LinkStore(state).for_user(user_id)
        return [link.chat_id] if link is not None else []

    def resolve(self, state: SqliteState, user_id: str, target_id: str) -> Any | None:
        link = LinkStore(state).for_user(user_id)
        return link.chat_id if link is not None and link.chat_id == target_id else None

    def send(self, message: Message, target: str) -> DeliveryResult:
        try:
            self._api.send_message(target, format_message(message, self._base_url))
        except TelegramApiError as exc:
            error = self.redact(str(exc))
            # 403: blocked or left. A 400 only when the chat is gone; any
            # other bad request (a message too long, ...) keeps the link.
            if exc.status == 403 or (exc.status == 400 and "chat not found" in error.lower()):
                return DeliveryResult.gone(error)
            if exc.retryable:
                return DeliveryResult.retry(error, exc.retry_after)
            return DeliveryResult.dead(error)
        return DeliveryResult.sent()

    def on_failed(
        self,
        state: SqliteState,
        user_id: str,
        target_id: str,
        result: DeliveryResult,
        now: datetime,
    ) -> None:
        if result.outcome != "gone":
            return
        link = LinkStore(state).for_user(user_id)
        if link is not None and link.chat_id == target_id:
            LinkStore(state).unlink_user(user_id, actor="service:telegram", reason="gone")
            _log.warning("telegram.unlinked_gone", user_id=user_id)

    def redact(self, text: str) -> str:
        token = bot_token()
        return text.replace(token, "***") if token else text
