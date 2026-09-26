"""``[connections]`` settings. Nothing is enabled by default: a provider
works only when an admin lists it in ``enabled_providers`` *and* its
app-level settings are present.

Until the integration step moves this into ``stonks.config.Settings``, it is
read from the same TOML file's ``[connections]`` table plus the environment:

- ``STONKS_CONNECTIONS_ENABLED_PROVIDERS``: comma-separated, overrides TOML.
- ``STONKS_SNAPTRADE_CLIENT_ID`` / ``STONKS_SNAPTRADE_CONSUMER_KEY``: the
  SnapTrade partner credentials. The consumer key is env-only (refused in
  TOML) and kept as a ``SecretStr``.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

ENABLED_ENV = "STONKS_CONNECTIONS_ENABLED_PROVIDERS"
SNAPTRADE_CLIENT_ID_ENV = "STONKS_SNAPTRADE_CLIENT_ID"
SNAPTRADE_CONSUMER_KEY_ENV = "STONKS_SNAPTRADE_CONSUMER_KEY"


class SnapTradeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    base_url: str = "https://api.snaptrade.com/api/v1"
    client_id: str | None = None
    consumer_key: SecretStr | None = None
    timeout_seconds: float = 20.0

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.consumer_key)


class ConnectionsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    #: Providers an admin turned on. Empty by default: nothing connects.
    enabled_providers: tuple[str, ...] = ()
    #: How long a connection-portal link (and its callback state) stays valid.
    portal_ttl_minutes: int = Field(default=30, ge=1, le=24 * 60)
    #: Threads fetching from providers during a scheduled sync.
    sync_workers: int = Field(default=4, ge=1, le=32)
    market_hours_sync_minutes: int = Field(default=15, ge=1)
    off_hours_sync_minutes: int = Field(default=60, ge=1)
    #: First sync pulls this much activity history; later syncs overlap a week.
    activity_lookback_days: int = Field(default=90, ge=1)
    snaptrade: SnapTradeConfig = Field(default_factory=SnapTradeConfig)

    @field_validator("enabled_providers", mode="before")
    @classmethod
    def _split(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.split(",")
        if isinstance(value, list | tuple):
            return tuple(dict.fromkeys(str(v).strip().lower() for v in value if str(v).strip()))
        return value

    def is_enabled(self, provider: str) -> bool:
        return provider in self.enabled_providers

    @classmethod
    def load(
        cls,
        config_path: Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> ConnectionsConfig:
        from stonks.config import DEFAULT_CONFIG_PATH

        env = os.environ if environ is None else environ
        path = Path(config_path) if config_path is not None else DEFAULT_CONFIG_PATH
        data: dict[str, Any] = {}
        if path.exists():
            with open(path, "rb") as fh:
                data = dict(tomllib.load(fh).get("connections", {}))
        snap = dict(data.get("snaptrade", {}))
        if "consumer_key" in snap:
            raise ValueError(
                "connections.snaptrade.consumer_key must not be set in config; "
                f"use {SNAPTRADE_CONSUMER_KEY_ENV}"
            )
        if env.get(ENABLED_ENV) is not None:
            data["enabled_providers"] = env[ENABLED_ENV]
        if env.get(SNAPTRADE_CLIENT_ID_ENV):
            snap["client_id"] = env[SNAPTRADE_CLIENT_ID_ENV]
        if env.get(SNAPTRADE_CONSUMER_KEY_ENV):
            snap["consumer_key"] = env[SNAPTRADE_CONSUMER_KEY_ENV]
        data["snaptrade"] = snap
        return cls(**data)
