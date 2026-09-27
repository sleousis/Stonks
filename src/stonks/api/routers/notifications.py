"""Web Push devices and per-user notification settings and feed (design
section 7). Every route takes the caller's scope, so each needs the bearer
token; push endpoints, keys and the webhook URL are write-only."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, Response

from stonks.api.deps import PageDep, ScopeDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES, ProblemDetails
from stonks.app.notifications import (
    FeedView,
    MarkReadRequest,
    MarkReadView,
    PreferencesUpdate,
    PreferencesView,
    PushDeviceView,
    PushSubscriptionRequest,
    PushUnsubscribeRequest,
    QuietHoursUpdate,
    TestNotificationView,
    VapidKeyView,
    WebhookUpdate,
)
from stonks.app.pagination import Page, page_of
from stonks.auth import Permission
from stonks.notify.service import FEED_LIMIT_MAX

push_router = APIRouter(prefix="/api/push", tags=["push"], responses=PROBLEM_RESPONSES)
router = APIRouter(prefix="/api/notifications", tags=["notifications"], responses=PROBLEM_RESPONSES)


# ---- Web Push ------------------------------------------------------------------------


@push_router.get("/vapid-key", response_model=VapidKeyView, operation_id="getVapidKey")
def vapid_key(services: ServicesDep, scope: ScopeDep) -> VapidKeyView:
    """The VAPID public key browsers subscribe with; null when this server
    can't send Web Push (the console then doesn't subscribe)."""
    return services.notifications.vapid_key()


@push_router.get(
    "/subscriptions", response_model=Page[PushDeviceView], operation_id="listPushSubscriptions"
)
def list_push_subscriptions(
    services: ServicesDep, scope: ScopeDep, page: PageDep
) -> Page[PushDeviceView]:
    """Your registered browsers and installed apps (no endpoints or keys)."""
    return page_of(services.notifications.devices(scope), page)


@push_router.post(
    "/subscriptions",
    status_code=201,
    response_model=PushDeviceView,
    operation_id="createPushSubscription",
    dependencies=needs(Permission.NOTIFICATIONS_MANAGE),
)
def create_push_subscription(
    body: PushSubscriptionRequest, services: ServicesDep, scope: ScopeDep
) -> PushDeviceView:
    """Register this browser (``PushSubscription.toJSON()``). The endpoint
    must be a known push service; re-registering refreshes it in place."""
    return services.notifications.subscribe(scope, body)


@push_router.delete(
    "/subscriptions",
    status_code=204,
    operation_id="deletePushSubscription",
    dependencies=needs(Permission.NOTIFICATIONS_MANAGE),
)
def delete_push_subscription(
    body: PushUnsubscribeRequest, services: ServicesDep, scope: ScopeDep
) -> Response:
    """Unregister a browser by its endpoint (404 when it isn't yours)."""
    services.notifications.unsubscribe(scope, body.endpoint)
    return Response(status_code=204)


@push_router.delete(
    "/subscriptions/{device_id}",
    status_code=204,
    operation_id="deletePushDevice",
    dependencies=needs(Permission.NOTIFICATIONS_MANAGE),
)
def delete_push_device(
    device_id: Annotated[str, Path(max_length=64)], services: ServicesDep, scope: ScopeDep
) -> Response:
    """Unregister one of your devices by the id the list shows, for
    removing another browser from Settings (404 when it isn't yours)."""
    services.notifications.remove_device(scope, device_id)
    return Response(status_code=204)


# ---- preferences ---------------------------------------------------------------------


@router.get(
    "/preferences", response_model=PreferencesView, operation_id="getNotificationPreferences"
)
def get_preferences(services: ServicesDep, scope: ScopeDep) -> PreferencesView:
    return services.notifications.preferences(scope)


@router.put(
    "/preferences",
    response_model=PreferencesView,
    operation_id="updateNotificationPreferences",
    dependencies=needs(Permission.NOTIFICATIONS_MANAGE),
)
def update_preferences(
    body: PreferencesUpdate, services: ServicesDep, scope: ScopeDep
) -> PreferencesView:
    """Set per-category (and optionally per-strategy) channel switches."""
    return services.notifications.update_preferences(scope, body)


@router.put(
    "/quiet-hours",
    response_model=PreferencesView,
    operation_id="setQuietHours",
    dependencies=needs(Permission.NOTIFICATIONS_MANAGE),
)
def set_quiet_hours(
    body: QuietHoursUpdate, services: ServicesDep, scope: ScopeDep
) -> PreferencesView:
    """Hold low and normal urgency deliveries for a morning digest; high
    urgency (halts, failed auto orders) always goes."""
    return services.notifications.set_quiet_hours(scope, body)


@router.put(
    "/webhook",
    response_model=PreferencesView,
    operation_id="setNotificationWebhook",
    dependencies=needs(Permission.NOTIFICATIONS_MANAGE),
)
def set_webhook(body: WebhookUpdate, services: ServicesDep, scope: ScopeDep) -> PreferencesView:
    """Your fallback webhook. Write-only: never shown back in full."""
    return services.notifications.set_webhook(scope, body)


# ---- feed ------------------------------------------------------------------------------


@router.get("", response_model=FeedView, operation_id="listNotifications")
def list_notifications(
    services: ServicesDep,
    scope: ScopeDep,
    unread_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=FEED_LIMIT_MAX)] = 50,
    before_id: Annotated[int | None, Query(ge=1, description="page: ids below this")] = None,
) -> FeedView:
    """Your in-app feed, newest first, with the unread count."""
    return services.notifications.feed(
        scope, unread_only=unread_only, limit=limit, before_id=before_id
    )


@router.post(
    "/read",
    response_model=MarkReadView,
    operation_id="markNotificationsRead",
    dependencies=needs(Permission.READ),
)
def mark_read(body: MarkReadRequest, services: ServicesDep, scope: ScopeDep) -> MarkReadView:
    """Mark notifications read (all of yours when ``ids`` is omitted). Only
    touches your own rows, so viewers may do it too."""
    return services.notifications.mark_read(scope, body)


@router.post(
    "/test",
    status_code=201,
    response_model=TestNotificationView,
    operation_id="sendTestNotification",
    dependencies=needs(Permission.READ),
    responses={429: {"model": ProblemDetails, "description": "Too Many Requests"}},
)
def send_test(services: ServicesDep, scope: ScopeDep) -> TestNotificationView:
    """Send yourself a test notification on every channel you turned on
    (push devices, email, your webhook), to check that alerts reach you.
    It skips quiet hours. Only ever reaches you, so viewers may do it too.
    One a minute: a second call in the same minute answers 429."""
    return services.notifications.send_test(scope)
