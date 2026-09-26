"""Generic JSON webhook notifier.

POSTs ``{"level", "title", "message", "fields", "text"}`` to a URL. ``text``
is a one-line rendering so chat tools that only read a text field (Slack
incoming webhooks, Mattermost, ...) show something useful unmodified.

The URL is treated as a secret (chat webhooks embed their token in the
path): it is never logged, never shown in ``repr``, and scrubbed from error
messages (``requests`` puts the full URL in its exception text).
"""

from __future__ import annotations

from typing import Any, Protocol

import requests

from stonks.logging import get_logger
from stonks.notify.base import Notification, Notifier, redact_url

_log = get_logger("stonks.notify.webhook")


class _Session(Protocol):
    def post(self, url: str, json: Any = ..., timeout: float = ..., headers: Any = ...) -> Any: ...


class WebhookNotifier(Notifier):
    def __init__(
        self,
        url: str,
        timeout_seconds: float = 5.0,
        session: _Session | None = None,
    ) -> None:
        self._url = url
        self._timeout = timeout_seconds
        self._session = session if session is not None else requests.Session()

    def __repr__(self) -> str:
        return f"WebhookNotifier(url={redact_url(self._url)!r}, timeout={self._timeout})"

    def notify(self, notification: Notification) -> None:
        # Own failure event (redacted) instead of the base class's generic one.
        try:
            self._send(notification)
        except Exception as exc:
            _log.error(
                "notify.webhook.failed",
                url=redact_url(self._url),
                title=notification.title,
                error=self._redact(str(exc)),
                error_type=type(exc).__name__,
            )

    def _send(self, notification: Notification) -> None:
        body = {
            "level": notification.level,
            "title": notification.title,
            "message": notification.message,
            "fields": notification.json_fields(),
            "text": f"[{notification.level.upper()}] {notification.title}: {notification.message}",
        }
        resp = self._session.post(
            self._url,
            json=body,
            timeout=self._timeout,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()

    def _redact(self, text: str) -> str:
        return text.replace(self._url, redact_url(self._url)) if self._url else text
