"""Notifier that writes alerts to the structured log."""

from __future__ import annotations

from stonks.logging import get_logger
from stonks.notify.base import Notification, Notifier

_log = get_logger("stonks.notify.log")


class LogNotifier(Notifier):
    def _send(self, notification: Notification) -> None:
        method = {"info": _log.info, "warning": _log.warning, "error": _log.error}[
            notification.level
        ]
        fields = {
            k: v
            for k, v in notification.json_fields().items()
            if k not in ("event", "level", "title", "message")
        }
        method("notify", title=notification.title, message=notification.message, **fields)
