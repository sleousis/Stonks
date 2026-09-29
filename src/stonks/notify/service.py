"""Scoped notification services: what the API routes (step S7) call.

Every function takes the caller's :class:`~stonks.accounts.scope.Scope` and
acts only on that user's rows: push devices, preferences, quiet hours, the
user's webhook and their in-app feed. Someone else's id reads as
:class:`~stonks.accounts.NotFound`, like a missing one. Service scopes
(scheduler, system) have no inbox and are refused. Changes are written to
``audit_log`` without secrets (no endpoints, keys or webhook paths).

Push endpoints are capability URLs the server will POST to, so they must be
``https`` on a known push service (FCM, Mozilla, Apple, Windows): anything
else could turn the delivery worker into a request forger.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from stonks.accounts import AccountsError, AuditLog, NotFound, Scope
from stonks.calendars.countries import COUNTRY_LABELS
from stonks.calendars.importance import IMPORTANCE_LABELS, IMPORTANCE_LEVELS
from stonks.notify.base import redact_url
from stonks.notify.channels import channel_defaults, channel_names, offered_channel_names
from stonks.notify.prefs import (
    EVENT_ALERT_TOPICS,
    EconomicAlertPrefs,
    EconomicAlertPrefStore,
    EventAlertPrefStore,
    Preference,
    PreferenceStore,
)
from stonks.notify.settings import OutboxSettings
from stonks.security.netguard import UnsafeAddress, check_public_host
from stonks.store.state import SqliteState

#: Hosts (and their subdomains) of the browser vendors' push services.
PUSH_SERVICE_HOSTS: tuple[str, ...] = (
    "fcm.googleapis.com",  # Chrome, Edge, Opera, Android
    "android.googleapis.com",
    "push.services.mozilla.com",  # Firefox
    "push.apple.com",  # Safari and iOS home-screen apps (web.push.apple.com)
    "notify.windows.com",  # legacy Edge / Windows
)

ENDPOINT_MAX = 2048
USER_AGENT_MAX = 256
WEBHOOK_MAX = 2048
FEED_LIMIT_MAX = 200


@dataclass(frozen=True)
class PushDevice:
    """A registered browser or app, as its owner sees it: no endpoint, no keys."""

    id: str
    endpoint_host: str
    user_agent: str | None
    created_at: str
    last_success_at: str | None
    failure_count: int


@dataclass(frozen=True)
class NotificationPreferences:
    preferences: tuple[Preference, ...]
    quiet_start: str | None
    quiet_end: str | None
    timezone: str
    webhook: str | None  # redacted: scheme and host only
    channels: tuple[str, ...] = field(default_factory=tuple)
    #: ``(channel, default_enabled, fallback)`` for every channel.
    channel_defaults: tuple[tuple[str, bool, bool], ...] = field(default_factory=tuple)
    #: One switch per upcoming-event alert kind, in display order.
    event_alerts: tuple[EventAlertSwitch, ...] = field(default_factory=tuple)
    #: Countries and importance threshold of economic release alerts.
    economic_alerts: EconomicAlertPrefs | None = None


@dataclass(frozen=True)
class EconomicAlertChange:
    """What to change in a person's economic release alerts. ``None``
    leaves a field as it is. ``default_countries`` goes back to the
    countries of the person's portfolios."""

    countries: tuple[str, ...] | None = None
    default_countries: bool = False
    min_importance: str | None = None

    @property
    def empty(self) -> bool:
        return self.countries is None and not self.default_countries and not self.min_importance


#: ``(code, label)`` of every country the console offers.
ECONOMIC_COUNTRY_OPTIONS: tuple[tuple[str, str], ...] = tuple(COUNTRY_LABELS.items())
#: ``(level, label)`` of every importance threshold, lowest first.
IMPORTANCE_OPTIONS: tuple[tuple[str, str], ...] = tuple(
    (level, IMPORTANCE_LABELS[level]) for level in IMPORTANCE_LEVELS
)


@dataclass(frozen=True)
class EventAlertSwitch:
    """Whether a person gets one kind of upcoming-event alert at all."""

    topic: str
    label: str
    enabled: bool


@dataclass(frozen=True)
class FeedItem:
    id: int
    category: str | None
    level: str
    title: str
    message: str
    deep_link: str | None
    created_at: str
    read_at: str | None


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _person(state: SqliteState, scope: Scope) -> str:
    """The scope's user id, if it is an active person."""
    if scope.is_service:
        raise AccountsError("service principals have no notifications")
    rows = state.sql(
        "SELECT 1 FROM users WHERE id = ? AND status = 'active' AND kind = 'human'",
        [scope.user_id],
    )
    if not rows:
        raise NotFound("user not found")
    return scope.user_id


def _host_is_public(host: str) -> bool:
    """Save-time host check: no local names, no non-public address in any
    numeric form. Names are checked again, resolved and pinned, on every
    webhook send (``WebhookChannel``); push hosts are also allow-listed."""
    try:
        check_public_host(host)
    except UnsafeAddress:
        return False
    return True


def _check_endpoint(endpoint: str) -> str:
    if not isinstance(endpoint, str) or len(endpoint) > ENDPOINT_MAX:
        raise AccountsError("push endpoint is missing or too long")
    parts = urlsplit(endpoint)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or parts.username or parts.password:
        raise AccountsError("push endpoint must be an https URL")
    if not _host_is_public(host) or not any(
        host == h or host.endswith("." + h) for h in PUSH_SERVICE_HOSTS
    ):
        raise AccountsError("push endpoint is not a known push service")
    return endpoint


def _decode_b64url(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError, TypeError) as exc:
        raise AccountsError("push keys must be base64url") from exc


def _check_keys(p256dh: str, auth: str) -> None:
    if not isinstance(p256dh, str) or not isinstance(auth, str):
        raise AccountsError("push keys are required")
    point = _decode_b64url(p256dh)
    if len(point) != 65 or point[0] != 4:
        raise AccountsError("p256dh must be an uncompressed P-256 public key")
    if len(_decode_b64url(auth)) != 16:
        raise AccountsError("auth must be 16 bytes")


# ---- devices -------------------------------------------------------------------


def register_push_subscription(
    state: SqliteState,
    scope: Scope,
    *,
    endpoint: str,
    p256dh: str,
    auth: str,
    user_agent: str | None = None,
    settings: OutboxSettings | None = None,
) -> PushDevice:
    """Store (or refresh) this browser's push subscription for the caller.

    A browser has one subscription per site, so an endpoint already on file
    is updated in place and moves to the caller if someone else registered
    it before (the same browser, signed in as another user). Beyond
    ``max_devices_per_user`` the least recently registered device is retired.
    """
    user_id = _person(state, scope)
    _check_endpoint(endpoint)
    _check_keys(p256dh, auth)
    limit = (settings or OutboxSettings()).max_devices_per_user
    agent = user_agent[:USER_AGENT_MAX] if user_agent else None
    now = _iso(_now())
    host = urlsplit(endpoint).hostname or ""
    audit = AuditLog(state)
    with state.transaction():
        rows = state.sql(
            "SELECT id, user_id FROM push_subscriptions WHERE endpoint = ?", [endpoint]
        )
        if rows:
            sub_id, previous = rows[0]["id"], rows[0]["user_id"]
            state.execute(
                "UPDATE push_subscriptions SET user_id = ?, p256dh = ?, auth = ?, user_agent = ?,"
                " created_at = ?, failure_count = 0, revoked_at = NULL, revoked_reason = NULL"
                " WHERE id = ?",
                [user_id, p256dh, auth, agent, now, sub_id],
            )
            if previous != user_id:
                audit.record(
                    scope.actor,
                    "notify.push.move",
                    "push_subscription",
                    sub_id,
                    details={"from_user": previous, "host": host},
                )
        else:
            sub_id = f"psh_{uuid.uuid4().hex[:16]}"
            state.execute(
                "INSERT INTO push_subscriptions (id, user_id, endpoint, p256dh, auth, user_agent,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [sub_id, user_id, endpoint, p256dh, auth, agent, now],
            )
        active = state.sql(
            "SELECT id FROM push_subscriptions WHERE user_id = ? AND revoked_at IS NULL"
            " ORDER BY created_at DESC, rowid DESC",
            [user_id],
        )
        for extra in active[limit:]:
            state.execute(
                "UPDATE push_subscriptions SET revoked_at = ?, revoked_reason = 'replaced'"
                " WHERE id = ?",
                [now, extra["id"]],
            )
        audit.record(
            scope.actor,
            "notify.push.register",
            "push_subscription",
            sub_id,
            details={"host": host},
        )
    return _device(state, user_id, sub_id)


def register_browser_subscription(
    state: SqliteState,
    scope: Scope,
    subscription: Mapping[str, Any],
    *,
    user_agent: str | None = None,
    settings: OutboxSettings | None = None,
) -> PushDevice:
    """:func:`register_push_subscription` from the browser's
    ``PushSubscription.toJSON()`` (``{endpoint, keys: {p256dh, auth}}``), the
    body of ``POST /api/push/subscriptions``."""
    if not isinstance(subscription, Mapping):
        raise AccountsError("expected a push subscription object")
    keys = subscription.get("keys")
    if not isinstance(keys, Mapping):
        raise AccountsError("push subscription has no keys")
    return register_push_subscription(
        state,
        scope,
        endpoint=subscription.get("endpoint"),  # type: ignore[arg-type]
        p256dh=keys.get("p256dh"),  # type: ignore[arg-type]
        auth=keys.get("auth"),  # type: ignore[arg-type]
        user_agent=user_agent,
        settings=settings,
    )


def remove_push_subscription_by_endpoint(state: SqliteState, scope: Scope, endpoint: str) -> None:
    """``DELETE /api/push/subscriptions`` (the browser knows its endpoint,
    not our id). Another user's endpoint reads as not found."""
    user_id = _person(state, scope)
    rows = state.sql(
        "SELECT id FROM push_subscriptions WHERE endpoint = ? AND user_id = ?"
        " AND revoked_at IS NULL",
        [endpoint, user_id],
    )
    if not rows:
        raise NotFound("push subscription not found")
    remove_push_subscription(state, scope, rows[0]["id"])


def remove_push_subscription(state: SqliteState, scope: Scope, subscription_id: str) -> None:
    user_id = _person(state, scope)
    with state.transaction():
        changed = state.execute(
            "UPDATE push_subscriptions SET revoked_at = ?, revoked_reason = 'user'"
            " WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
            [_iso(_now()), subscription_id, user_id],
        ).rowcount
        if not changed:
            raise NotFound("push subscription not found")
        AuditLog(state).record(
            scope.actor, "notify.push.remove", "push_subscription", subscription_id
        )


def list_push_subscriptions(state: SqliteState, scope: Scope) -> list[PushDevice]:
    user_id = _person(state, scope)
    rows = state.sql(
        "SELECT * FROM push_subscriptions WHERE user_id = ? AND revoked_at IS NULL"
        " ORDER BY created_at, rowid",
        [user_id],
    )
    return [_to_device(r) for r in rows]


def _device(state: SqliteState, user_id: str, sub_id: str) -> PushDevice:
    rows = state.sql(
        "SELECT * FROM push_subscriptions WHERE id = ? AND user_id = ?", [sub_id, user_id]
    )
    return _to_device(rows[0])


def _to_device(r) -> PushDevice:
    return PushDevice(
        id=r["id"],
        endpoint_host=urlsplit(r["endpoint"]).hostname or "",
        user_agent=r["user_agent"],
        created_at=r["created_at"],
        last_success_at=r["last_success_at"],
        failure_count=int(r["failure_count"]),
    )


# ---- preferences -----------------------------------------------------------------


def get_preferences(state: SqliteState, scope: Scope) -> NotificationPreferences:
    user_id = _person(state, scope)
    store = PreferenceStore(state)
    s = store.settings(user_id)
    offered = offered_channel_names()
    return NotificationPreferences(
        preferences=tuple(store.list(user_id)),
        quiet_start=s.quiet_start,
        quiet_end=s.quiet_end,
        timezone=s.timezone,
        webhook=redact_url(s.webhook_url) if s.webhook_url else None,
        channels=tuple(offered),
        channel_defaults=tuple(
            (name, enabled, fallback)
            for name, (enabled, fallback) in channel_defaults().items()
            if name in offered
        ),
        event_alerts=tuple(
            EventAlertSwitch(topic, EVENT_ALERT_TOPICS[topic], enabled)
            for topic, enabled in EventAlertPrefStore(state).switches(user_id).items()
        ),
        economic_alerts=EconomicAlertPrefStore(state).get(user_id),
    )


def update_preferences(
    state: SqliteState,
    scope: Scope,
    preferences: Iterable[Preference],
    *,
    event_alerts: Mapping[str, bool] | None = None,
    economic: EconomicAlertChange | None = None,
) -> NotificationPreferences:
    """Channel switches per category (and strategy), the per-kind event
    alert switches, and the countries and importance threshold of economic
    release alerts. Only what is given changes."""
    user_id = _person(state, scope)
    prefs = list(preferences)
    switches = dict(event_alerts or {})
    economic = economic if economic is not None and not economic.empty else None
    for topic in switches:
        if topic not in EVENT_ALERT_TOPICS:
            raise AccountsError(
                f"unknown event alert kind {topic!r}; choose from {list(EVENT_ALERT_TOPICS)}"
            )
    known = set(channel_names())
    for p in prefs:
        if p.channel not in known:
            raise AccountsError(f"unknown channel {p.channel!r}")
        if p.strategy_id is not None and not state.sql(
            "SELECT 1 FROM strategies WHERE id = ?", [p.strategy_id]
        ):
            raise AccountsError(f"unknown strategy {p.strategy_id!r}")
    with state.transaction():
        now = _now()
        PreferenceStore(state).set(user_id, prefs, now=now)
        EventAlertPrefStore(state).set(user_id, switches, now=now)
        if economic is not None:
            try:
                EconomicAlertPrefStore(state).set(
                    user_id,
                    countries=economic.countries,
                    default_countries=economic.default_countries,
                    min_importance=economic.min_importance,
                    now=now,
                )
            except ValueError as exc:
                raise AccountsError(str(exc)) from exc
        details: dict[str, Any] = {
            "changes": [[p.category, p.strategy_id, p.channel, p.enabled] for p in prefs]
        }
        if switches:
            details["event_alerts"] = switches
        if economic is not None:
            details["economic_alerts"] = {
                "countries": list(economic.countries) if economic.countries else None,
                "default_countries": economic.default_countries,
                "min_importance": economic.min_importance,
            }
        AuditLog(state).record(scope.actor, "notify.prefs.update", "user", user_id, details=details)
    return get_preferences(state, scope)


def set_quiet_hours(
    state: SqliteState, scope: Scope, start: str | None, end: str | None
) -> NotificationPreferences:
    """Quiet hours as ``HH:MM`` in the user's time zone; both None clears them."""
    user_id = _person(state, scope)
    with state.transaction():
        try:
            PreferenceStore(state).set_quiet_hours(user_id, start, end, now=_now())
        except ValueError as exc:
            raise AccountsError(str(exc)) from exc
        AuditLog(state).record(
            scope.actor,
            "notify.quiet_hours",
            "user",
            user_id,
            details={"start": start, "end": end},
        )
    return get_preferences(state, scope)


def set_webhook(state: SqliteState, scope: Scope, url: str | None) -> NotificationPreferences:
    """The user's own webhook (a secret: stored, never shown or logged in full)."""
    user_id = _person(state, scope)
    if url is not None:
        parts = urlsplit(url) if isinstance(url, str) else None
        if (
            parts is None
            or len(url) > WEBHOOK_MAX
            or parts.scheme != "https"
            or not parts.hostname
            or not _host_is_public(parts.hostname)
        ):
            raise AccountsError("webhook must be a public https URL")
    with state.transaction():
        PreferenceStore(state).set_webhook(user_id, url, now=_now())
        AuditLog(state).record(
            scope.actor,
            "notify.webhook",
            "user",
            user_id,
            details={"webhook": redact_url(url) if url else None},
        )
    return get_preferences(state, scope)


# ---- feed ------------------------------------------------------------------------


def list_notifications(
    state: SqliteState,
    scope: Scope,
    *,
    unread_only: bool = False,
    limit: int = 50,
    before_id: int | None = None,
) -> list[FeedItem]:
    """The caller's in-app feed, newest first; page with ``before_id``."""
    user_id = _person(state, scope)
    if not 1 <= limit <= FEED_LIMIT_MAX:
        raise AccountsError(f"limit must be 1-{FEED_LIMIT_MAX}")
    sql = "SELECT * FROM alerts WHERE user_id = ?"
    params: list = [user_id]
    if unread_only:
        sql += " AND read_at IS NULL"
    if before_id is not None:
        sql += " AND id < ?"
        params.append(before_id)
    rows = state.sql(sql + " ORDER BY id DESC LIMIT ?", [*params, limit])
    return [_feed_item(r) for r in rows]


def _feed_item(r) -> FeedItem:
    try:
        link = json.loads(r["context_json"] or "{}").get("deep_link")
    except (ValueError, AttributeError):
        link = None
    return FeedItem(
        id=int(r["id"]),
        category=r["category"],
        level=r["level"],
        title=r["title"],
        message=r["message"],
        deep_link=link if isinstance(link, str) else None,
        created_at=r["created_at"],
        read_at=r["read_at"],
    )


def mark_read(state: SqliteState, scope: Scope, ids: Iterable[int] | None = None) -> int:
    """Mark the caller's notifications read (all of them when ``ids`` is None).
    Ids that aren't the caller's are ignored. Returns how many changed."""
    user_id = _person(state, scope)
    now = _iso(_now())
    if ids is None:
        return state.execute(
            "UPDATE alerts SET read_at = ? WHERE user_id = ? AND read_at IS NULL", [now, user_id]
        ).rowcount
    wanted = sorted({int(i) for i in ids})
    if not wanted:
        return 0
    marks = ", ".join("?" for _ in wanted)
    return state.execute(
        f"UPDATE alerts SET read_at = ? WHERE user_id = ? AND read_at IS NULL AND id IN ({marks})",
        [now, user_id, *wanted],
    ).rowcount


def unread_count(state: SqliteState, scope: Scope) -> int:
    user_id = _person(state, scope)
    rows = state.sql("SELECT COUNT(*) FROM alerts WHERE user_id = ? AND read_at IS NULL", [user_id])
    return int(rows[0][0])
