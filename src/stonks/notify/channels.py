"""Delivery channels: the seam every way of reaching a user implements, and
the ``@register_channel(name)`` registry.

The in-app feed isn't a channel: the router writes its ``alerts`` row in the
producer's transaction, always. Channels here are the asynchronous ones the
delivery worker drives: ``webpush``, ``email``, ``webhook`` (the user's own)
and ``log``.

A channel never raises for a delivery problem; it returns a
:class:`DeliveryResult` the worker turns into sent, retry-with-backoff or
dead. Errors are redacted by the channel (it knows its own secrets) before
they reach a log line or the ``last_error`` column.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar, Literal
from urllib.parse import urlsplit

import requests

from stonks.logging import get_logger
from stonks.notify.base import Notification
from stonks.notify.events import Message
from stonks.notify.settings import NotifySettings
from stonks.notify.webhook import RedirectRefused, WebhookNotifier, _Session
from stonks.security.netguard import Resolver, UnsafeAddress, pinned_session, resolve_public
from stonks.store.state import SqliteState

Outcome = Literal["sent", "retry", "dead", "gone"]

_log = get_logger("stonks.notify.channels")


@dataclass(frozen=True)
class DeliveryResult:
    """``sent``; ``retry`` (transient, try again later); ``dead`` (permanent,
    give up); ``gone`` (the target no longer exists: revoke it)."""

    outcome: Outcome
    error: str | None = None
    retry_after: float | None = None

    @classmethod
    def sent(cls) -> DeliveryResult:
        return cls("sent")

    @classmethod
    def retry(cls, error: str, after: float | None = None) -> DeliveryResult:
        return cls("retry", error, after)

    @classmethod
    def dead(cls, error: str) -> DeliveryResult:
        return cls("dead", error)

    @classmethod
    def gone(cls, error: str) -> DeliveryResult:
        return cls("gone", error)


class Channel(ABC):
    name: ClassVar[str] = ""
    #: On for every category unless the user turns it off.
    default_enabled: ClassVar[bool] = False
    #: Used for ``high`` urgency when the user has no working push device.
    fallback: ClassVar[bool] = False

    @classmethod
    def from_settings(cls, settings: NotifySettings) -> Channel | None:
        """The configured channel, or None when it can't run (no keys...)."""
        return cls()

    def targets(self, state: SqliteState, user_id: str) -> list[str]:
        """Target ids for one user; one delivery row is written per target.
        Single-target channels return ``[""]`` when the user can be reached."""
        return [""] if self.resolve(state, user_id, "") is not None else []

    def resolve(self, state: SqliteState, user_id: str, target_id: str) -> Any | None:
        """What :meth:`send` needs for a target, or None if it's gone."""
        return user_id

    @abstractmethod
    def send(self, message: Message, target: Any) -> DeliveryResult: ...

    def on_sent(  # noqa: B027 - optional hook
        self, state: SqliteState, user_id: str, target_id: str, now: datetime
    ) -> None:
        """Hook after a successful send (e.g. reset a failure counter)."""

    def on_failed(  # noqa: B027 - optional hook
        self,
        state: SqliteState,
        user_id: str,
        target_id: str,
        result: DeliveryResult,
        now: datetime,
    ) -> None:
        """Hook after a failed send (``retry``, ``dead`` or ``gone``)."""

    def redact(self, text: str) -> str:
        return text


_REGISTRY: dict[str, type[Channel]] = {}


def register_channel(name: str) -> Callable[[type[Channel]], type[Channel]]:
    def decorator(cls: type[Channel]) -> type[Channel]:
        if not (isinstance(cls, type) and issubclass(cls, Channel)):
            raise TypeError(f"@register_channel({name!r}) needs a Channel subclass")
        if name in _REGISTRY and _REGISTRY[name] is not cls:
            raise ValueError(f"channel {name!r} is already registered")
        cls.name = name
        _REGISTRY[name] = cls
        return cls

    return decorator


def _load_builtins() -> None:
    # Imported for their @register_channel side effect.
    from stonks.notify import smtp, webpush  # noqa: F401
    from stonks.telegram import channel  # noqa: F401


def channel_names() -> list[str]:
    _load_builtins()
    return sorted(_REGISTRY)


def channel_defaults() -> dict[str, tuple[bool, bool]]:
    """Each channel's ``(default_enabled, fallback)``: whether it is on for
    a category the user never set, and whether it stands in for push on
    ``high`` urgency."""
    _load_builtins()
    return {
        name: (bool(cls.default_enabled), bool(cls.fallback))
        for name, cls in sorted(_REGISTRY.items())
    }


def build_channels(settings: NotifySettings) -> dict[str, Channel]:
    """Every registered channel that is configured, by name."""
    _load_builtins()
    out: dict[str, Channel] = {}
    for name, cls in sorted(_REGISTRY.items()):
        channel = cls.from_settings(settings)
        if channel is not None:
            out[name] = channel
    return out


@register_channel("log")
class LogChannel(Channel):
    """Writes a line to the structured log (user id, category, title only).
    Off by default; operators turn it on per user for debugging."""

    def send(self, message: Message, target: Any) -> DeliveryResult:
        _log.info(
            "notify.deliver",
            user_id=message.user_id,
            notification_id=message.notification_id,
            category=message.category,
            urgency=message.urgency,
            title=message.title,
        )
        return DeliveryResult.sent()


@register_channel("webhook")
class WebhookChannel(Channel):
    """POSTs to the user's own webhook (``notification_settings.webhook_url``).
    Off by default; a fallback for ``high`` urgency when the user has no
    working push device. The URL is a secret and never leaves this class
    unredacted.

    The user picks the host, so each send resolves it, refuses unless every
    address is public, and connects to that checked address (no DNS
    rebinding). Redirects are never followed (review finding AS-05)."""

    fallback = True

    def __init__(
        self,
        timeout_seconds: float = 5.0,
        session: _Session | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self._timeout = timeout_seconds
        #: Tests inject a fake session; otherwise each send pins a new one.
        self._session = session
        self._resolver = resolver

    def resolve(self, state: SqliteState, user_id: str, target_id: str) -> str | None:
        rows = state.sql(
            "SELECT webhook_url FROM notification_settings WHERE user_id = ?", [user_id]
        )
        return rows[0]["webhook_url"] if rows and rows[0]["webhook_url"] else None

    def send(self, message: Message, target: str) -> DeliveryResult:
        parts = urlsplit(target)
        redact = WebhookNotifier(url=target)._redact
        try:
            kwargs = {"resolver": self._resolver} if self._resolver is not None else {}
            address = resolve_public(parts.hostname or "", parts.port or 443, **kwargs)
        except UnsafeAddress as exc:
            return DeliveryResult.dead(redact(f"refused: {exc}"))
        session = self._session if self._session is not None else pinned_session(address)
        notifier = WebhookNotifier(
            url=target, timeout_seconds=self._timeout, session=session, follow_redirects=False
        )
        fields = {"category": message.category, "deep_link": message.deep_link}
        try:
            notifier._send(
                Notification(
                    level=message.level, title=message.title, message=message.body, fields=fields
                )
            )
        except RedirectRefused as exc:
            return DeliveryResult.dead(notifier._redact(str(exc)))
        except requests.HTTPError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            error = notifier._redact(f"HTTP {status}: {exc}")
            if status == 429 or (status is not None and status >= 500):
                return DeliveryResult.retry(error)
            return DeliveryResult.dead(error)
        except Exception as exc:
            return DeliveryResult.retry(notifier._redact(f"{type(exc).__name__}: {exc}"))
        return DeliveryResult.sent()
