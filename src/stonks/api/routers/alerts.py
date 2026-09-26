from __future__ import annotations

from fastapi import APIRouter

from stonks.api.deps import PageDep, ServicesDep
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.alerts import AlertView
from stonks.app.pagination import Page
from stonks.notify import NotificationLevel

router = APIRouter(prefix="/api/alerts", tags=["alerts"], responses=PROBLEM_RESPONSES)


@router.get("", response_model=Page[AlertView], operation_id="listAlerts")
def list_alerts(
    services: ServicesDep, page: PageDep, level: NotificationLevel | None = None
) -> Page[AlertView]:
    """Every notification the ``store`` backend recorded, newest first."""
    return services.alerts.list(level=level, limit=page.limit, offset=page.offset)
