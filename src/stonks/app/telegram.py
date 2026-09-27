"""TelegramService: your chat link and link codes, plus the hosted bot
(roadmap 20.3). See :mod:`stonks.telegram`.

Every call is about the caller's own link. Making a code or unlinking
needs ``notifications.manage``, reading the link needs ``data.read``.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from stonks.app.context import AppContext
from stonks.app.errors import ConfigurationError
from stonks.auth.policy import Permission, require
from stonks.auth.principal import Principal
from stonks.logging import get_logger
from stonks.telegram.bot import HostedTelegramBot
from stonks.telegram.links import LinkStore
from stonks.telegram.settings import TelegramConfig, bot_configured

_log = get_logger("stonks.app.telegram")


class TelegramLinkView(BaseModel):
    bot_configured: bool = Field(description="The server has a bot token, so it can send.")
    bot_enabled: bool = Field(description="The server answers commands ([telegram].enabled).")
    bot_username: str | None = None
    linked: bool
    username: str | None = Field(default=None, description="The linked chat's @username.")
    linked_at: datetime | None = None


class TelegramLinkCodeView(BaseModel):
    code: str = Field(description="Shown once. Send /link CODE to the bot.")
    expires_at: datetime
    bot_username: str | None = None


class TelegramService:
    def __init__(self, context: AppContext) -> None:
        self._ctx = context
        self._hosted: HostedTelegramBot | None = None

    @property
    def config(self) -> TelegramConfig:
        return getattr(self._ctx.settings, "telegram", None) or TelegramConfig()

    def link(self, principal: Principal) -> TelegramLinkView:
        require(principal, Permission.READ)
        with self._ctx.state() as state:
            link = LinkStore(state).for_user(principal.user_id)
        return TelegramLinkView(
            bot_configured=bot_configured(),
            bot_enabled=self.config.enabled,
            bot_username=self.config.bot_username,
            linked=link is not None,
            username=link.username if link else None,
            linked_at=link.linked_at if link else None,
        )

    def create_code(self, principal: Principal) -> TelegramLinkCodeView:
        """A one-time code for linking a chat to the caller."""
        require(principal, Permission.NOTIFICATIONS_MANAGE)
        if not bot_configured():
            raise ConfigurationError(
                "the Telegram bot is not set up on this server (STONKS_TELEGRAM_BOT_TOKEN)"
            )
        with self._ctx.state() as state:
            code, expires = LinkStore(state).create_code(
                principal.user_id, minutes=self.config.link_code_minutes, actor=principal.actor
            )
        return TelegramLinkCodeView(
            code=code, expires_at=expires, bot_username=self.config.bot_username
        )

    def unlink(self, principal: Principal) -> bool:
        require(principal, Permission.NOTIFICATIONS_MANAGE)
        with self._ctx.state() as state:
            return LinkStore(state).unlink_user(principal.user_id, actor=principal.actor)

    # ---- the hosted bot inside stonks serve ------------------------------------------

    def start_hosted(self) -> bool:
        if not self.config.enabled:
            return False
        self._hosted = HostedTelegramBot(self._ctx, self.config)
        return self._hosted.start()

    def stop_hosted(self) -> None:
        if self._hosted is not None:
            self._hosted.stop()
            self._hosted = None
