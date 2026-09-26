"""NotificationsAppService: the transport-facing view of
:mod:`stonks.notify.service` (Web Push devices, preferences, quiet hours,
the user's webhook and the in-app feed).

Thin on purpose: each call opens a state connection and runs the scoped
notify function. Push endpoints and keys, and the webhook URL, go in and
never come back out (devices show the push-service host only; the webhook
shows scheme and host).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from stonks.accounts import AccountsError, NotFound, Scope
from stonks.app.context import AppContext
from stonks.app.errors import NotFoundError, ValidationError
from stonks.notify import service as notify
from stonks.notify.events import Category
from stonks.notify.prefs import Preference
from stonks.notify.service import ENDPOINT_MAX, FEED_LIMIT_MAX, USER_AGENT_MAX, WEBHOOK_MAX
from stonks.notify.settings import NotifySettings
from stonks.notify.webpush import vapid_public_key
from stonks.store.state import SqliteState

# ---- request models ---------------------------------------------------------------


class PushKeys(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    p256dh: str = Field(min_length=1, max_length=256)
    auth: str = Field(min_length=1, max_length=64)


class PushSubscriptionRequest(BaseModel):
    """The browser's ``PushSubscription.toJSON()`` plus its user agent."""

    model_config = ConfigDict(hide_input_in_errors=True)

    endpoint: str = Field(min_length=1, max_length=ENDPOINT_MAX)
    keys: PushKeys
    expirationTime: float | None = None  # the browser's field name
    user_agent: str | None = Field(default=None, max_length=USER_AGENT_MAX)


class PushUnsubscribeRequest(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    endpoint: str = Field(min_length=1, max_length=ENDPOINT_MAX)


class PreferenceItem(BaseModel):
    category: Category
    channel: str = Field(min_length=1, max_length=64)
    enabled: bool
    #: One strategy only; omitted: every strategy.
    strategy_id: str | None = Field(default=None, max_length=200)


class PreferencesUpdate(BaseModel):
    preferences: list[PreferenceItem] = Field(max_length=200)


class QuietHoursUpdate(BaseModel):
    #: ``HH:MM`` in your time zone; both null clears quiet hours.
    start: str | None = Field(default=None, max_length=5)
    end: str | None = Field(default=None, max_length=5)


class WebhookUpdate(BaseModel):
    """Your own webhook (a public ``https`` URL). Write-only: responses show
    its scheme and host only. Null removes it."""

    model_config = ConfigDict(hide_input_in_errors=True)

    url: SecretStr | None = Field(default=None, max_length=WEBHOOK_MAX)


class MarkReadRequest(BaseModel):
    #: Notification ids to mark read; omitted: all of them.
    ids: list[int] | None = Field(default=None, max_length=FEED_LIMIT_MAX)


# ---- views ---------------------------------------------------------------------------


class VapidKeyView(BaseModel):
    #: base64url VAPID public key, or null when the server can't send Web Push.
    public_key: str | None


class PushDeviceView(BaseModel):
    """A registered browser or installed app: no endpoint, no keys."""

    id: str
    endpoint_host: str
    user_agent: str | None
    created_at: datetime
    last_success_at: datetime | None
    failure_count: int


class ChannelDefaultView(BaseModel):
    channel: str
    #: On for a category you never set.
    default_enabled: bool
    #: Used instead of push for high urgency when you have no working device.
    fallback: bool


class PreferencesView(BaseModel):
    preferences: list[PreferenceItem]
    quiet_start: str | None
    quiet_end: str | None
    timezone: str
    #: Scheme and host of your webhook (the rest is a secret), or null.
    webhook: str | None
    #: Channels a preference can name.
    channels: list[str]
    #: What each channel does when you have not set it.
    channel_defaults: list[ChannelDefaultView] = Field(default_factory=list)


class FeedItemView(BaseModel):
    id: int
    category: str | None
    level: str
    title: str
    message: str
    deep_link: str | None
    created_at: datetime
    read_at: datetime | None


class FeedView(BaseModel):
    items: list[FeedItemView]
    unread_count: int


class MarkReadView(BaseModel):
    updated: int
    unread_count: int


# ---- service --------------------------------------------------------------------------


@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except NotFound as exc:
        raise NotFoundError(str(exc)) from None
    except (AccountsError, ValueError) as exc:
        raise ValidationError(str(exc)) from None


def _device(d: notify.PushDevice) -> PushDeviceView:
    return PushDeviceView(
        id=d.id,
        endpoint_host=d.endpoint_host,
        user_agent=d.user_agent,
        created_at=d.created_at,  # type: ignore[arg-type]
        last_success_at=d.last_success_at,  # type: ignore[arg-type]
        failure_count=d.failure_count,
    )


def _prefs(p: notify.NotificationPreferences) -> PreferencesView:
    return PreferencesView(
        preferences=[
            PreferenceItem(
                category=x.category, channel=x.channel, enabled=x.enabled, strategy_id=x.strategy_id
            )
            for x in p.preferences
        ],
        quiet_start=p.quiet_start,
        quiet_end=p.quiet_end,
        timezone=p.timezone,
        webhook=p.webhook,
        channels=list(p.channels),
        channel_defaults=[
            ChannelDefaultView(channel=name, default_enabled=enabled, fallback=fallback)
            for name, enabled, fallback in p.channel_defaults
        ],
    )


class NotificationsAppService:
    def __init__(self, context: AppContext, *, settings: NotifySettings | None = None) -> None:
        """``settings`` defaults to ``settings.notify`` when it is a
        :class:`NotifySettings`, else the ``STONKS_VAPID_*`` / ``STONKS_NOTIFY_*``
        environment."""
        self._ctx = context
        self._settings = settings

    @property
    def settings(self) -> NotifySettings:
        if self._settings is None:
            existing = getattr(self._ctx.settings, "notify", None)
            self._settings = (
                existing if isinstance(existing, NotifySettings) else NotifySettings.from_env()
            )
        return self._settings

    @contextmanager
    def _state(self) -> Iterator[SqliteState]:
        with self._ctx.state() as state, _errors():
            yield state

    # ---- push ---------------------------------------------------------------------

    def vapid_key(self) -> VapidKeyView:
        return VapidKeyView(public_key=vapid_public_key(self.settings))

    def subscribe(self, scope: Scope, request: PushSubscriptionRequest) -> PushDeviceView:
        with self._state() as state:
            device = notify.register_push_subscription(
                state,
                scope,
                endpoint=request.endpoint,
                p256dh=request.keys.p256dh,
                auth=request.keys.auth,
                user_agent=request.user_agent,
                settings=self.settings.outbox,
            )
        return _device(device)

    def unsubscribe(self, scope: Scope, endpoint: str) -> None:
        with self._state() as state:
            notify.remove_push_subscription_by_endpoint(state, scope, endpoint)

    def devices(self, scope: Scope) -> list[PushDeviceView]:
        with self._state() as state:
            return [_device(d) for d in notify.list_push_subscriptions(state, scope)]

    # ---- preferences --------------------------------------------------------------

    def preferences(self, scope: Scope) -> PreferencesView:
        with self._state() as state:
            return _prefs(notify.get_preferences(state, scope))

    def update_preferences(self, scope: Scope, request: PreferencesUpdate) -> PreferencesView:
        with self._state() as state:
            prefs = [
                Preference(
                    category=p.category,
                    channel=p.channel,
                    enabled=p.enabled,
                    strategy_id=p.strategy_id,
                )
                for p in request.preferences
            ]
            return _prefs(notify.update_preferences(state, scope, prefs))

    def set_quiet_hours(self, scope: Scope, request: QuietHoursUpdate) -> PreferencesView:
        with self._state() as state:
            return _prefs(notify.set_quiet_hours(state, scope, request.start, request.end))

    def set_webhook(self, scope: Scope, request: WebhookUpdate) -> PreferencesView:
        url = request.url.get_secret_value() if request.url is not None else None
        with self._state() as state:
            return _prefs(notify.set_webhook(state, scope, url))

    # ---- feed ----------------------------------------------------------------------

    def feed(
        self, scope: Scope, *, unread_only: bool, limit: int, before_id: int | None
    ) -> FeedView:
        with self._state() as state:
            items = notify.list_notifications(
                state, scope, unread_only=unread_only, limit=limit, before_id=before_id
            )
            unread = notify.unread_count(state, scope)
        return FeedView(
            items=[
                FeedItemView(
                    id=i.id,
                    category=i.category,
                    level=i.level,
                    title=i.title,
                    message=i.message,
                    deep_link=i.deep_link,
                    created_at=i.created_at,  # type: ignore[arg-type]
                    read_at=i.read_at,  # type: ignore[arg-type]
                )
                for i in items
            ],
            unread_count=unread,
        )

    def mark_read(self, scope: Scope, request: MarkReadRequest) -> MarkReadView:
        with self._state() as state:
            updated = notify.mark_read(state, scope, request.ids)
            unread = notify.unread_count(state, scope)
        return MarkReadView(updated=int(updated), unread_count=unread)
