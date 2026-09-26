"""Notifier seam: the ABC every alert backend implements.

``Notifier.notify`` is the only public entry point and it never raises:
alerting is a side channel, so a broken webhook must not fail the tick or
health check that tried to report something. Backends implement ``_send``
and may raise freely; the base class logs and swallows.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

from stonks.logging import get_logger

NotificationLevel = Literal["info", "warning", "error"]

LEVEL_ORDER: dict[str, int] = {"info": 10, "warning": 20, "error": 30}

_log = get_logger("stonks.notify")


@dataclass(frozen=True)
class Notification:
    level: NotificationLevel
    title: str
    message: str
    fields: Mapping[str, Any] = field(default_factory=dict)

    def json_fields(self) -> dict[str, Any]:
        """``fields`` with every value coerced to something JSON can encode."""
        out: dict[str, Any] = {}
        for key, value in self.fields.items():
            try:
                json.dumps(value)
                out[str(key)] = value
            except (TypeError, ValueError):
                out[str(key)] = str(value)
        return out


class Notifier(ABC):
    #: True for backends that record every notification (the alerts store):
    #: :class:`CompositeNotifier` then skips its ``min_level`` filter for them.
    receives_all_levels: bool = False

    def notify(self, notification: Notification) -> None:
        try:
            self._send(notification)
        except Exception as exc:
            _log.error(
                "notify.failed",
                notifier=type(self).__name__,
                title=notification.title,
                error=self._redact(str(exc)),
                error_type=type(exc).__name__,
            )

    @abstractmethod
    def _send(self, notification: Notification) -> None: ...

    def _redact(self, text: str) -> str:
        """Hook for backends holding secrets to scrub them from error text."""
        return text


class CompositeNotifier(Notifier):
    """Fans out to children, dropping notifications below ``min_level``
    (except for children that set ``receives_all_levels``). Each child is isolated: one failing backend doesn't stop the others."""

    def __init__(
        self, children: Sequence[Notifier], min_level: NotificationLevel = "warning"
    ) -> None:
        self.children = list(children)
        self.min_level = min_level

    def _send(self, notification: Notification) -> None:
        below = LEVEL_ORDER[notification.level] < LEVEL_ORDER[self.min_level]
        for child in self.children:
            if below and not child.receives_all_levels:
                continue
            child.notify(notification)


def redact_url(url: str) -> str:
    """Keep only scheme and host: webhook paths and queries embed tokens."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "***"
    if not parts.scheme or not parts.hostname:
        return "***"
    return f"{parts.scheme}://{parts.hostname}/***"
