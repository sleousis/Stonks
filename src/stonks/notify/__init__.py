"""Alerting seam (roadmap 2.5): ``Notifier`` ABC plus log, webhook and store backends."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

from stonks.config import NotifyConfig, Settings, configured_secrets
from stonks.logging import get_logger
from stonks.notify.base import (
    CompositeNotifier,
    Notification,
    NotificationLevel,
    Notifier,
    redact_url,
)
from stonks.notify.log import LogNotifier
from stonks.notify.store import StoreNotifier
from stonks.notify.webhook import WebhookNotifier

__all__ = [
    "CompositeNotifier",
    "LogNotifier",
    "Notification",
    "NotificationLevel",
    "Notifier",
    "StoreNotifier",
    "WebhookNotifier",
    "build_notifier",
    "notifier_from_settings",
    "redact_url",
]

_log = get_logger("stonks.notify")


def build_notifier(
    config: NotifyConfig,
    *,
    state_path: str | Path | None = None,
    secrets: Callable[[], Iterable[str]] = tuple,
) -> CompositeNotifier:
    """The configured backends behind one :class:`CompositeNotifier`.

    ``state_path`` is the state DB the ``store`` backend writes to (skipped,
    with a warning, when it is not given); ``secrets`` returns the credential
    values it scrubs before writing.
    """
    children: list[Notifier] = []
    for backend in config.backends:
        if backend == "log":
            children.append(LogNotifier())
        elif backend == "webhook":
            if not config.webhook.url:
                _log.warning(
                    "notify.webhook.unconfigured",
                    hint="set STONKS_NOTIFY_WEBHOOK_URL or [notify.webhook].url",
                )
                continue
            children.append(
                WebhookNotifier(
                    url=config.webhook.url, timeout_seconds=config.webhook.timeout_seconds
                )
            )
        elif backend == "store":
            if state_path is None:
                _log.warning("notify.store.unconfigured", hint="no state DB path given")
                continue
            children.append(StoreNotifier(state_path, secrets=secrets))
    return CompositeNotifier(children, min_level=config.min_level)


def notifier_from_settings(settings: Settings) -> CompositeNotifier:
    """``[notify]`` wired to the settings' state DB and credentials."""
    return build_notifier(
        settings.notify,
        state_path=settings.state.path,
        secrets=lambda: configured_secrets(settings),
    )
