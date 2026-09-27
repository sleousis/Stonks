"""An in-memory :class:`TelegramApi` for tests and local development.

Queue incoming messages with :meth:`FakeTelegramApi.push`, read what the
bot sent from :attr:`FakeTelegramApi.sent`, and make sends fail with
:attr:`FakeTelegramApi.send_error`. ``get_updates`` honours the offset the
way Telegram does: an offset confirms (drops) every earlier update.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from stonks.telegram.api import TelegramApi, TelegramApiError, TelegramUpdate


@dataclass
class FakeTelegramApi(TelegramApi):
    pending: list[TelegramUpdate] = field(default_factory=list)
    sent: list[tuple[str, str]] = field(default_factory=list)
    #: Raised by the next sends while set.
    send_error: TelegramApiError | None = None
    #: Raised by the next ``get_updates`` while set.
    poll_error: Exception | None = None
    offsets: list[int | None] = field(default_factory=list)
    _next_id: int = 1

    def push(
        self, chat_id: str | int, text: str, *, username: str | None = None, chat_type="private"
    ) -> TelegramUpdate:
        update = TelegramUpdate(self._next_id, str(chat_id), text, username, chat_type)
        self._next_id += 1
        self.pending.append(update)
        return update

    def get_updates(self, offset: int | None, timeout: int) -> list[TelegramUpdate]:
        self.offsets.append(offset)
        if self.poll_error is not None:
            raise self.poll_error
        if offset is not None:
            self.pending = [u for u in self.pending if u.update_id >= offset]
        return list(self.pending)

    def send_message(self, chat_id: str, text: str) -> None:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append((str(chat_id), text))

    def replies(self, chat_id: str | int) -> list[str]:
        return [text for chat, text in self.sent if chat == str(chat_id)]
