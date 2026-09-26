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
from urllib.parse import urlsplit

import requests

from stonks.logging import get_logger
from stonks.notify.base import Notification, Notifier, redact_url

_log = get_logger("stonks.notify.webhook")


class RedirectRefused(Exception):
    """The endpoint answered with a redirect and the notifier does not follow them."""


class _Session(Protocol):
    def post(
        self, url: str, json: Any = ..., timeout: float = ..., headers: Any = ..., **kwargs: Any
    ) -> Any: ...


class WebhookNotifier(Notifier):
    def __init__(
        self,
        url: str,
        timeout_seconds: float = 5.0,
        session: _Session | None = None,
        *,
        follow_redirects: bool = True,
    ) -> None:
        self._url = url
        self._timeout = timeout_seconds
        self._session = session if session is not None else requests.Session()
        self._follow_redirects = follow_redirects

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
        extra: dict[str, Any] = {} if self._follow_redirects else {"allow_redirects": False}
        resp = self._session.post(
            self._url,
            json=body,
            timeout=self._timeout,
            headers={"Content-Type": "application/json"},
            **extra,
        )
        if not self._follow_redirects and 300 <= int(resp.status_code) < 400:
            raise RedirectRefused(f"HTTP {resp.status_code}: redirects are not followed")
        resp.raise_for_status()

    def _redact(self, text: str) -> str:
        """Scrub the URL and every secret-bearing fragment of it: urllib3
        errors name only the path (``... with url: /services/T0/B0/TOKEN``),
        so replacing the full URL alone would leak the token."""
        if not self._url:
            return text
        parts = urlsplit(self._url)
        replacements = {self._url: redact_url(self._url)}
        if parts.query:
            replacements[f"{parts.path}?{parts.query}"] = "/***"
            replacements[parts.query] = "***"
        if parts.path not in ("", "/"):
            replacements[parts.path] = "/***"
        if parts.password:
            replacements[parts.password] = "***"
        # Longest first, so the full URL wins over its own path.
        for fragment in sorted(replacements, key=len, reverse=True):
            text = text.replace(fragment, replacements[fragment])
        return text
