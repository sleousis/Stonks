"""Auth settings, env-only for now (``STONKS_AUTH_*``). The integration step
may move them under ``[auth]`` in ``config/default.toml``."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AuthSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STONKS_AUTH_", extra="ignore")

    #: Send cookies with ``Secure``. Browsers accept it on http://localhost too.
    cookie_secure: bool = True
    session_idle_hours: float = Field(default=12.0, gt=0)
    session_absolute_days: float = Field(default=7.0, gt=0)
    #: How long a password-only session may wait for its second factor.
    pending_login_minutes: float = Field(default=10.0, gt=0)
    #: Sensitive actions need a second factor verified this recently.
    step_up_minutes: float = Field(default=10.0, gt=0)
    #: Failed attempts allowed per account and per IP in the window.
    max_failures: int = Field(default=5, ge=1)
    failure_window_minutes: float = Field(default=15.0, gt=0)
    totp_issuer: str = "Stonks"
