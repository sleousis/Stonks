from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from stonks.api.deps import PageDep, PrincipalDep, ServicesDep
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.alerts import AlertView
from stonks.app.pagination import Page
from stonks.notify import NotificationLevel
from stonks.notify.events import Category

router = APIRouter(prefix="/api/alerts", tags=["alerts"], responses=PROBLEM_RESPONSES)


@router.get("", response_model=Page[AlertView], operation_id="listAlerts")
def list_alerts(
    services: ServicesDep,
    principal: PrincipalDep,
    page: PageDep,
    level: NotificationLevel | None = None,
    category: Annotated[list[Category] | None, Query()] = None,
) -> Page[AlertView]:
    """Your notifications, newest first. Admins also see the admin audience
    (operational alerts with no single recipient). ``category`` keeps only
    those categories (repeat it for more). ``system`` also keeps the
    operator alerts, which carry no category."""
    return services.alerts.list(
        principal, level=level, categories=category, limit=page.limit, offset=page.offset
    )
