"""Long polling (roadmap 20.3): no public webhook, so a home server works.

:class:`TelegramBot` asks ``getUpdates`` for everything after the stored
offset (``telegram_bot_state``), answers each message through the
:class:`~stonks.telegram.commands.CommandHandler`, and stores the next
offset after each one. A restart therefore never answers a message twice.
One bad update is logged and skipped, never fatal.

:class:`HostedTelegramBot` runs the loop on a daemon thread inside
``stonks serve`` when ``[telegram].enabled`` is true and the token is set.
``stonks telegram poll`` runs the same loop in the foreground.
"""

from __future__ import annotations

import threading

from stonks.app.context import AppContext
from stonks.logging import get_logger
from stonks.telegram.api import HttpTelegramApi, TelegramApi, TelegramApiError
from stonks.telegram.commands import CommandHandler
from stonks.telegram.links import LinkStore
from stonks.telegram.settings import TelegramConfig, bot_token

_log = get_logger("stonks.telegram.bot")


class TelegramBot:
    def __init__(
        self,
        api: TelegramApi,
        context: AppContext,
        config: TelegramConfig | None = None,
        *,
        handler: CommandHandler | None = None,
    ) -> None:
        self._api = api
        self._ctx = context
        self._config = config or TelegramConfig()
        self._handler = handler or CommandHandler(context, self._config)

    def poll_once(self, timeout: int | None = None) -> int:
        """One ``getUpdates`` round. Returns how many updates were handled."""
        with self._ctx.state() as state:
            offset = LinkStore(state).get_offset()
        wait = self._config.poll_timeout_seconds if timeout is None else timeout
        updates = self._api.get_updates(offset, wait)
        for update in sorted(updates, key=lambda u: u.update_id):
            if offset is not None and update.update_id < offset:
                continue
            try:
                reply = self._handler.handle(update)
                if reply:
                    self._api.send_message(update.chat_id, reply)
            except Exception as exc:
                _log.error(
                    "telegram.update_failed",
                    update_id=update.update_id,
                    error_type=type(exc).__name__,
                    error=self._redact(str(exc)),
                )
            offset = update.update_id + 1
            with self._ctx.state() as state:
                LinkStore(state).set_offset(offset)
        return len(updates)

    def _redact(self, text: str) -> str:
        token = bot_token()
        return text.replace(token, "***") if token else text


def build_bot(context: AppContext, config: TelegramConfig) -> TelegramBot | None:
    """The bot over the real Bot API, or ``None`` without a token."""
    token = bot_token()
    if token is None:
        return None
    api = HttpTelegramApi(token, base_url=config.api_base_url, timeout_seconds=10.0)
    return TelegramBot(api, context, config)


class HostedTelegramBot:
    """The poll loop on a daemon thread (``start`` / ``stop``)."""

    def __init__(
        self, context: AppContext, config: TelegramConfig, *, bot: TelegramBot | None = None
    ) -> None:
        self._ctx = context
        self._config = config
        self._bot = bot
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        """Start polling when enabled and configured. Returns whether it runs."""
        if self.running or not self._config.enabled:
            return self.running
        bot = self._bot or build_bot(self._ctx, self._config)
        if bot is None:
            _log.warning("telegram.not_started", reason="STONKS_TELEGRAM_BOT_TOKEN is not set")
            return False
        self._bot = bot
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="telegram-bot", daemon=True)
        self._thread.start()
        _log.info("telegram.started")
        return True

    def stop(self, timeout: float = 1.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        self._thread = None

    def _run(self) -> None:
        assert self._bot is not None
        while not self._stop.is_set():
            try:
                self._bot.poll_once()
            except TelegramApiError as exc:
                _log.warning("telegram.poll_failed", status=exc.status, error=str(exc))
                self._stop.wait(exc.retry_after or self._config.error_backoff_seconds)
            except Exception as exc:
                _log.error("telegram.poll_crashed", error_type=type(exc).__name__)
                self._stop.wait(self._config.error_backoff_seconds)
