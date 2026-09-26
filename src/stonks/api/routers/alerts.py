from __future__ import annotations

from fastapi import APIRouter

from stonks.api.deps import PageDep, PrincipalDep, ServicesDep
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.alerts import AlertView
from stonks.app.pagination import Page
from stonks.notify import NotificationLevel

router = APIRouter(prefix="/api/alerts", tags=["alerts"], responses=PROBLEM_RESPONSES)


@router.get("", response_model=Page[AlertView], operation_id="listAlerts")
def list_alerts(
    services: ServicesDep,
    principal: PrincipalDep,
    page: PageDep,
    level: NotificationLevel | None = None,
) -> Page[AlertView]:
    """Your notifications, newest first. Admins also see the admin audience
    (operational alerts with no single recipient)."""
    return services.alerts.list(principal, level=level, limit=page.limit, offset=page.offset)
