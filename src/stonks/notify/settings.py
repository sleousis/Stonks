"""Settings for the notification router, outbox and channels.

Read from the environment so secrets never land in a TOML file:

- ``STONKS_VAPID_PUBLIC_KEY`` / ``STONKS_VAPID_PRIVATE_KEY`` /
  ``STONKS_VAPID_SUBJECT`` (``mailto:`` or ``https:`` contact the push
  services require). Generate a pair with ``python -m stonks.notify vapid-keygen``.
- ``STONKS_SMTP_HOST`` / ``_PORT`` / ``_USERNAME`` / ``_PASSWORD`` /
  ``_FROM`` / ``_SECURITY`` (``starttls``, ``ssl`` or ``none``). Email is an
  optional fallback channel; it is off unless a host and sender are set.
- ``STONKS_NOTIFY_*`` for outbox tuning (retries, dedupe window, ...).

The integration step can nest these models under ``[notify]`` in
``config.py``; they are plain pydantic models too.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class OutboxSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STONKS_NOTIFY_", extra="ignore")

    #: Hours a dedupe key blocks repeats; None = forever (a signal key
    #: carries its as-of date, so a re-run tick never sends twice).
    dedupe_window_hours: float | None = Field(default=None, gt=0)
    max_attempts: int = Field(default=6, ge=1)
    backoff_base_seconds: float = Field(default=30.0, gt=0)
    backoff_max_seconds: float = Field(default=3600.0, gt=0)
    #: How long a worker holds a claimed delivery before another may retry it.
    lease_seconds: float = Field(default=120.0, gt=0)
    batch_size: int = Field(default=100, ge=1)
    #: Consecutive failures after which a push subscription is disabled.
    push_failure_limit: int = Field(default=5, ge=1)
    max_devices_per_user: int = Field(default=10, ge=1)
    #: Public origin of the console, for absolute links in email.
    public_base_url: str | None = None


class WebPushSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STONKS_VAPID_", extra="ignore")

    public_key: str | None = None  # base64url uncompressed P-256 point
    private_key: SecretStr | None = None  # base64url raw 32-byte scalar (or DER)
    subject: str | None = None  # mailto:ops@example.com or https://...
    timeout_seconds: float = Field(default=10.0, gt=0)

    @property
    def configured(self) -> bool:
        return bool(self.public_key and self.private_key and self.subject)


class SmtpSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="STONKS_SMTP_", extra="ignore", populate_by_name=True
    )

    host: str | None = None
    port: int = 587
    username: str | None = None
    password: SecretStr | None = None
    sender: str | None = Field(default=None, validation_alias="STONKS_SMTP_FROM")
    security: Literal["starttls", "ssl", "none"] = "starttls"
    timeout_seconds: float = Field(default=10.0, gt=0)

    @property
    def configured(self) -> bool:
        return bool(self.host and self.sender)


class NotifySettings(BaseModel):
    outbox: OutboxSettings = Field(default_factory=OutboxSettings)
    webpush: WebPushSettings = Field(default_factory=WebPushSettings)
    smtp: SmtpSettings = Field(default_factory=SmtpSettings)

    @classmethod
    def from_env(cls) -> NotifySettings:
        return cls(outbox=OutboxSettings(), webpush=WebPushSettings(), smtp=SmtpSettings())

    def secrets(self) -> list[str]:
        """Credential values to scrub from anything logged or stored."""
        values = [
            self.webpush.private_key.get_secret_value() if self.webpush.private_key else None,
            self.smtp.password.get_secret_value() if self.smtp.password else None,
        ]
        return [v for v in values if v]
