"""Auth settings: the ``[auth]`` section of ``config/default.toml``
(:class:`stonks.config.AuthConfig`), with ``STONKS_AUTH_<FIELD>`` env
overrides applied by :func:`stonks.config.load_settings`."""

from __future__ import annotations

from stonks.config import AuthConfig as AuthSettings

__all__ = ["AuthSettings"]
