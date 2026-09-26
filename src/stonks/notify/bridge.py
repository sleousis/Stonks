"""OutboxNotifier: the existing :class:`Notifier` seam, routed to users.

Tick, health and ops code already report through ``Notifier.notify``. This
backend turns each such notification into a ``system`` event for every
active admin, so it reaches their devices through the outbox (Web Push and
fallbacks) instead of only the global log, webhook and alerts table. A
``dedupe_key`` in the notification's fields is honoured. Nothing but the
title and message travels: ``fields`` stay in the log and the admin alerts
feed, never in a push payload.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from stonks.notify.base import Notification, Notifier
from stonks.notify.channels import Channel
from stonks.notify.events import Audience, Event
from stonks.notify.router import NotificationRouter
from stonks.notify.settings import OutboxSettings
from stonks.notify.store import redact_text
from stonks.store.state import SqliteState


class OutboxNotifier(Notifier):
    def __init__(
        self,
        state_path: str | Path,
        channels: Mapping[str, Channel],
        settings: OutboxSettings | None = None,
        *,
        secrets: Callable[[], Iterable[str]] = tuple,
    ) -> None:
        self._path = Path(state_path)
        self._channels = dict(channels)
        self._settings = settings
        self._secrets = secrets

    def __repr__(self) -> str:
        return f"OutboxNotifier(state_path={self._path.as_posix()!r})"

    def _send(self, notification: Notification) -> None:
        key = notification.fields.get("dedupe_key")
        with SqliteState(self._path) as state:
            router = NotificationRouter(
                state, self._channels, self._settings, secrets=self._secrets
            )
            router.publish(
                Event(
                    category="system",
                    title=notification.title,
                    body=notification.message,
                    audience=Audience.admins(),
                    level=notification.level,
                    dedupe_key=str(key) if key else None,
                )
            )

    def _redact(self, text: str) -> str:
        return redact_text(text, [s for s in self._secrets() if s])
