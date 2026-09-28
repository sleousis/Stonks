"""Screen alerts (roadmap 23.17): a saved screen that runs on a schedule
and notifies you about names that newly match, through your notification
channels. Notify only. Another person's screen is a 404, admins included."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Path, Query, Response
from pydantic import BaseModel, ConfigDict

from stonks.api.deps import PageDep, PrincipalDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.pagination import Page, page_of
from stonks.app.screen_alerts import (
    ScreenAlertEventView,
    ScreenAlertRunView,
    ScreenAlertService,
    ScreenAlertSet,
    ScreenAlertView,
)
from stonks.auth import Permission

router = APIRouter(prefix="/api/screener", tags=["screener"], responses=PROBLEM_RESPONSES)

ScreenId = Annotated[str, Path(max_length=64)]


class ScreenAlertRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: date | None = None


def _service(services: ServicesDep) -> ScreenAlertService:
    # Tests may put a configured service on ``services.screen_alerts``.
    found = getattr(services, "screen_alerts", None)
    return found if isinstance(found, ScreenAlertService) else ScreenAlertService(services.context)


@router.get("/alerts", response_model=Page[ScreenAlertView], operation_id="listScreenAlerts")
def list_screen_alerts(
    services: ServicesDep, principal: PrincipalDep, page: PageDep
) -> Page[ScreenAlertView]:
    """Your screen alerts, oldest first, with the last day each ran."""
    return page_of(_service(services).list(principal), page)


@router.get(
    "/alerts/events",
    response_model=Page[ScreenAlertEventView],
    operation_id="listScreenAlertEvents",
)
def list_screen_alert_events(
    services: ServicesDep,
    principal: PrincipalDep,
    page: PageDep,
    screen_id: Annotated[str | None, Query(max_length=64)] = None,
) -> Page[ScreenAlertEventView]:
    """When your screens found new names, newest first."""
    return _service(services).events(
        principal, screen_id=screen_id, limit=page.limit, offset=page.offset
    )


@router.post(
    "/alerts/evaluate",
    response_model=ScreenAlertRunView,
    operation_id="evaluateScreenAlerts",
    dependencies=needs(Permission.OPERATIONS_RUN),
)
def evaluate_screen_alerts(
    services: ServicesDep, principal: PrincipalDep, body: ScreenAlertRunRequest | None = None
) -> ScreenAlertRunView:
    """Run every due screen alert of every person now (the scheduler runs
    this after each price ingest)."""
    return _service(services).evaluate_view(principal, body.as_of if body else None)


@router.get(
    "/screens/{screen_id}/alert", response_model=ScreenAlertView, operation_id="getScreenAlert"
)
def get_screen_alert(
    screen_id: ScreenId, services: ServicesDep, principal: PrincipalDep
) -> ScreenAlertView:
    """The alert on one of your saved screens (404 when it has none)."""
    return _service(services).get(principal, screen_id)


@router.put(
    "/screens/{screen_id}/alert",
    response_model=ScreenAlertView,
    operation_id="setScreenAlert",
    dependencies=needs(Permission.NOTIFICATIONS_MANAGE),
)
def set_screen_alert(
    screen_id: ScreenId, body: ScreenAlertSet, services: ServicesDep, principal: PrincipalDep
) -> ScreenAlertView:
    """Turn the alert on for a saved screen, daily or weekly, or change it.
    The first run stores the matches and sends nothing."""
    return _service(services).set(principal, screen_id, body)


@router.delete(
    "/screens/{screen_id}/alert",
    status_code=204,
    response_class=Response,
    operation_id="deleteScreenAlert",
    dependencies=needs(Permission.NOTIFICATIONS_MANAGE),
)
def delete_screen_alert(
    screen_id: ScreenId, services: ServicesDep, principal: PrincipalDep
) -> Response:
    """Remove the alert (the screen stays)."""
    _service(services).delete(principal, screen_id)
    return Response(status_code=204)
