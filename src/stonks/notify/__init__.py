"""Alerting seam (roadmap 2.5) and per-user notifications (roadmap 15.6).

- ``Notifier`` ABC plus log, webhook and store backends: global alerts.
- :class:`NotificationRouter` -> ``notification_outbox`` -> :class:`DeliveryWorker`
  -> channels (``webpush``, ``email``, ``webhook``, ``log``) for per-user
  delivery with preferences, quiet hours, dedupe and retries.
- :mod:`stonks.notify.service`: scoped functions for the API routes.
- :func:`notify_signal`: what the signal phase calls.
"""

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
from stonks.notify.bridge import OutboxNotifier
from stonks.notify.channels import Channel, DeliveryResult, build_channels, register_channel
from stonks.notify.events import Audience, Event, Message
from stonks.notify.log import LogNotifier
from stonks.notify.router import (
    NotificationRouter,
    PublishResult,
    SignalNotice,
    notify_signal,
    notify_signals,
)
from stonks.notify.settings import NotifySettings
from stonks.notify.store import StoreNotifier
from stonks.notify.webhook import WebhookNotifier
from stonks.notify.worker import DeliveryWorker

__all__ = [
    "Audience",
    "Channel",
    "CompositeNotifier",
    "DeliveryResult",
    "DeliveryWorker",
    "Event",
    "LogNotifier",
    "Message",
    "Notification",
    "NotificationLevel",
    "NotificationRouter",
    "Notifier",
    "NotifySettings",
    "OutboxNotifier",
    "PublishResult",
    "SignalNotice",
    "StoreNotifier",
    "WebhookNotifier",
    "build_channels",
    "build_notifier",
    "notifier_from_settings",
    "notify_signal",
    "notify_signals",
    "redact_url",
    "register_channel",
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
        elif backend == "outbox":
            if state_path is None:
                _log.warning("notify.outbox.unconfigured", hint="no state DB path given")
                continue
            children.append(_outbox_notifier(state_path, secrets))
    return CompositeNotifier(children, min_level=config.min_level)


def _outbox_notifier(
    state_path: str | Path, secrets: Callable[[], Iterable[str]]
) -> OutboxNotifier:
    """Admins' outbox over the channels the environment configures."""
    notify = NotifySettings.from_env()
    return OutboxNotifier(
        state_path,
        build_channels(notify),
        notify.outbox,
        secrets=lambda: [*secrets(), *notify.secrets()],
    )


def notifier_from_settings(settings: Settings) -> CompositeNotifier:
    """``[notify]`` wired to the settings' state DB and credentials."""
    return build_notifier(
        settings.notify,
        state_path=settings.state.path,
        secrets=lambda: configured_secrets(settings),
    )
