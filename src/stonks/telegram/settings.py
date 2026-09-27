"""``[telegram]`` settings (roadmap 20.3).

The bot token is a secret and lives only in the environment
(``STONKS_TELEGRAM_BOT_TOKEN``). The bot polls only when ``enabled`` is
true *and* the token is set. The ``telegram`` notification channel only
needs the token (sending never needs polling).
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

TOKEN_ENV = "STONKS_TELEGRAM_BOT_TOKEN"


class TelegramConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Run the long-poll bot inside ``stonks serve`` (needs the env token).
    enabled: bool = False
    #: Long-poll wait per ``getUpdates`` call. Telegram allows up to 50.
    poll_timeout_seconds: int = Field(default=25, ge=0, le=50)
    #: How long a one-time link code stays valid.
    link_code_minutes: float = Field(default=10.0, gt=0, le=1440)
    #: How long ``/kill`` waits for its typed confirmation.
    kill_confirm_minutes: float = Field(default=2.0, gt=0, le=60)
    #: The bot's @username, shown next to a link code (optional).
    bot_username: str | None = None
    api_base_url: str = "https://api.telegram.org"
    #: Sleep after a failed poll before trying again.
    error_backoff_seconds: float = Field(default=5.0, gt=0)


def bot_token(env: Mapping[str, str] | None = None) -> str | None:
    """The bot token from the environment, or ``None``."""
    value = (os.environ if env is None else env).get(TOKEN_ENV, "").strip()
    return value or None


def bot_configured(env: Mapping[str, str] | None = None) -> bool:
    return bot_token(env) is not None
