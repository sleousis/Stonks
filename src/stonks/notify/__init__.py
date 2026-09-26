"""Alerting seam (roadmap 2.5): ``Notifier`` ABC plus log and webhook backends."""

from __future__ import annotations

from stonks.config import NotifyConfig
from stonks.logging import get_logger
from stonks.notify.base import (
    CompositeNotifier,
    Notification,
    NotificationLevel,
    Notifier,
    redact_url,
)
from stonks.notify.log import LogNotifier
from stonks.notify.webhook import WebhookNotifier

__all__ = [
    "CompositeNotifier",
    "LogNotifier",
    "Notification",
    "NotificationLevel",
    "Notifier",
    "WebhookNotifier",
    "build_notifier",
    "redact_url",
]

_log = get_logger("stonks.notify")


def build_notifier(config: NotifyConfig) -> CompositeNotifier:
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
    return CompositeNotifier(children, min_level=config.min_level)
