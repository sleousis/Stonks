"""What producers publish (:class:`Event`) and what channels send (:class:`Message`).

Payloads are minimal by design: a title, one line and an app-relative deep
link. No amounts, holdings or free-form context: push services, mail relays
and webhooks see what we send, and details load in the app after login. So
an event has no ``fields`` bag at all, and its text is flattened to one
line and capped.
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from typing import Literal, get_args

from stonks.notify.base import NotificationLevel

Category = Literal["signal", "order", "risk", "system"]
Urgency = Literal["low", "normal", "high"]
AudienceKind = Literal["users", "owner", "admins", "subscribers"]

CATEGORIES: tuple[str, ...] = get_args(Category)
URGENCIES: tuple[str, ...] = get_args(Urgency)
LEVELS: tuple[str, ...] = get_args(NotificationLevel)

TITLE_MAX = 80
BODY_MAX = 240
DEEP_LINK_MAX = 512
DEDUPE_KEY_MAX = 200

#: How long a push service keeps an undelivered message (design: signals
#: 12 h, risk 24 h).
TTL_SECONDS: dict[str, int] = {
    "signal": 12 * 3600,
    "order": 24 * 3600,
    "risk": 24 * 3600,
    "system": 24 * 3600,
}

_WS = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_DEEP_LINK = re.compile(r"^/(?![/\\])[^\s\\]*$")


def clean_text(text: str, limit: int) -> str:
    """One line, control characters removed, capped at ``limit`` characters."""
    flat = _WS.sub(" ", _CONTROL.sub(" ", str(text))).strip()
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def check_deep_link(link: str | None) -> str | None:
    """An app-relative path (``/signals?...``) or None. Absolute or
    protocol-relative URLs are refused: a notification must never be a
    phishing link, and the service worker opens it on click."""
    if link is None:
        return None
    if len(link) > DEEP_LINK_MAX or not _DEEP_LINK.match(link):
        raise ValueError("deep_link must be an app-relative path like /signals")
    return link


def push_topic(dedupe_key: str | None) -> str | None:
    """Web Push ``Topic`` for a dedupe key: at most 32 URL-safe base64
    characters (RFC 8030), and a hash, so the key's content (tickers,
    strategy ids) isn't shown to the push service."""
    if not dedupe_key:
        return None
    digest = hashlib.sha256(dedupe_key.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")[:32]


@dataclass(frozen=True)
class Audience:
    """Who an event is for. Resolved by the router to active human users."""

    kind: AudienceKind
    user_ids: tuple[str, ...] = ()
    portfolio_id: str | None = None
    strategy_id: str | None = None
    modes: tuple[str, ...] = ()

    @classmethod
    def users(cls, *user_ids: str) -> Audience:
        if not user_ids:
            raise ValueError("Audience.users needs at least one user id")
        return cls(kind="users", user_ids=tuple(user_ids))

    @classmethod
    def owner_of(cls, portfolio_id: str) -> Audience:
        """The owner of a portfolio (orders, fills, risk events)."""
        return cls(kind="owner", portfolio_id=portfolio_id)

    @classmethod
    def admins(cls) -> Audience:
        return cls(kind="admins")

    @classmethod
    def subscribers(cls, strategy_id: str, modes: tuple[str, ...] = ("notify",)) -> Audience:
        """Users with an enabled subscription to ``strategy_id`` in ``modes``."""
        return cls(kind="subscribers", strategy_id=strategy_id, modes=tuple(modes))


@dataclass(frozen=True)
class Event:
    category: Category
    title: str
    body: str
    audience: Audience
    level: NotificationLevel = "info"
    urgency: Urgency | None = None  # None: derived from category and level
    dedupe_key: str | None = None
    deep_link: str | None = None
    strategy_id: str | None = None
    portfolio_id: str | None = None

    def __post_init__(self) -> None:
        if self.category not in CATEGORIES:
            raise ValueError(f"unknown category {self.category!r}")
        if self.level not in LEVELS:
            raise ValueError(f"unknown level {self.level!r}")
        if self.urgency is not None and self.urgency not in URGENCIES:
            raise ValueError(f"unknown urgency {self.urgency!r}")
        title = clean_text(self.title, TITLE_MAX)
        if not title:
            raise ValueError("a notification needs a title")
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "body", clean_text(self.body, BODY_MAX))
        object.__setattr__(self, "deep_link", check_deep_link(self.deep_link))
        if self.dedupe_key is not None:
            key = self.dedupe_key.strip()
            if not key or len(key) > DEDUPE_KEY_MAX:
                raise ValueError("dedupe_key must be 1-200 characters")
            object.__setattr__(self, "dedupe_key", key)
        if self.urgency is None:
            high = self.category == "risk" or self.level == "error"
            object.__setattr__(self, "urgency", "high" if high else "normal")

    @property
    def ttl_seconds(self) -> int:
        return TTL_SECONDS[self.category]


@dataclass(frozen=True)
class Message:
    """One notification as a channel sends it."""

    notification_id: int | None
    user_id: str
    category: Category
    level: NotificationLevel
    urgency: Urgency
    title: str
    body: str
    deep_link: str | None
    dedupe_key: str | None
    ttl_seconds: int
    #: >1 when this message is a quiet-hours digest of that many notifications.
    digest_count: int = 1

    @property
    def topic(self) -> str | None:
        return push_topic(self.dedupe_key)

    @classmethod
    def digest(cls, user_id: str, count: int, ttl_seconds: int) -> Message:
        return cls(
            notification_id=None,
            user_id=user_id,
            category="system",
            level="info",
            urgency="normal",
            title=f"{count} notifications while you were away",
            body="Open Stonks to see them.",
            deep_link="/notifications",
            dedupe_key=None,
            ttl_seconds=ttl_seconds,
            digest_count=count,
        )
