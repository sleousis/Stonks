"""A Telegram Bot API stand-in for the e2e server that the test can read.

``tests.e2e.serve`` installs it when ``STONKS_E2E_TELEGRAM_LOG`` names a
file: every message the server sends to Telegram (the notification channel
and the bot) is appended there as one JSON line, and the bot's polls get
no updates. Nothing reaches api.telegram.org.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from stonks.telegram.api import TelegramApi, TelegramUpdate

LOG_ENV = "STONKS_E2E_TELEGRAM_LOG"

_LOCK = threading.Lock()


class FileTelegramApi(TelegramApi):
    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def get_updates(self, offset: int | None, timeout: int) -> list[TelegramUpdate]:
        return []

    def send_message(self, chat_id: str, text: str) -> None:
        line = json.dumps({"chat_id": str(chat_id), "text": text})
        with _LOCK, self._path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def read_sent(path: Path) -> list[dict[str, Any]]:
    """Every message the server sent, oldest first."""
    if not Path(path).is_file():
        return []
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]
