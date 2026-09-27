"""Your price alerts (roadmap 20.2): rules on a ticker or a watchlist,
checked after each data refresh and sent through your notification
channels. Another person's rule is a 404, admins included."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Path, Query, Response
from pydantic import BaseModel, ConfigDict

from stonks.api.deps import PageDep, PrincipalDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.pagination import Page, page_of
from stonks.app.price_alerts import (
    PriceAlertCreate,
    PriceAlertEventView,
    PriceAlertRunView,
    PriceAlertUpdate,
    PriceAlertView,
)
from stonks.auth import Permission

router = APIRouter(prefix="/api/price-alerts", tags=["price-alerts"], responses=PROBLEM_RESPONSES)

AlertId = Annotated[str, Path(max_length=64)]


class PriceAlertRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: date | None = None


@router.get("", response_model=Page[PriceAlertView], operation_id="listPriceAlerts")
def list_price_alerts(
    services: ServicesDep, principal: PrincipalDep, page: PageDep
) -> Page[PriceAlertView]:
    """Your price alert rules, oldest first, with the price each last saw."""
    return page_of(services.price_alerts.list(principal), page)


@router.post(
    "",
    status_code=201,
    response_model=PriceAlertView,
    operation_id="createPriceAlert",
    dependencies=needs(Permission.NOTIFICATIONS_MANAGE),
)
def create_price_alert(
    body: PriceAlertCreate, services: ServicesDep, principal: PrincipalDep
) -> PriceAlertView:
    """A new rule: crosses_above or crosses_below a level, or moves_pct by a
    percent over window_days, on a ticker or on one of your watchlists."""
    return services.price_alerts.create(principal, body)


@router.get(
    "/events", response_model=Page[PriceAlertEventView], operation_id="listPriceAlertEvents"
)
def list_price_alert_events(
    services: ServicesDep,
    principal: PrincipalDep,
    page: PageDep,
    rule_id: Annotated[str | None, Query(max_length=64)] = None,
) -> Page[PriceAlertEventView]:
    """When your rules fired, newest first."""
    return services.price_alerts.events(
        principal, rule_id=rule_id, limit=page.limit, offset=page.offset
    )


@router.post(
    "/evaluate",
    response_model=PriceAlertRunView,
    operation_id="evaluatePriceAlerts",
    dependencies=needs(Permission.OPERATIONS_RUN),
)
def evaluate_price_alerts(
    services: ServicesDep, principal: PrincipalDep, body: PriceAlertRunRequest | None = None
) -> PriceAlertRunView:
    """Check every enabled rule of every person against the latest closes
    now (the scheduler runs this after each price ingest)."""
    return services.price_alerts.evaluate_view(principal, body.as_of if body else None)


@router.get("/{alert_id}", response_model=PriceAlertView, operation_id="getPriceAlert")
def get_price_alert(
    alert_id: AlertId, services: ServicesDep, principal: PrincipalDep
) -> PriceAlertView:
    return services.price_alerts.get(principal, alert_id)


@router.patch(
    "/{alert_id}",
    response_model=PriceAlertView,
    operation_id="updatePriceAlert",
    dependencies=needs(Permission.NOTIFICATIONS_MANAGE),
)
def update_price_alert(
    alert_id: AlertId, body: PriceAlertUpdate, services: ServicesDep, principal: PrincipalDep
) -> PriceAlertView:
    """Rename a rule, change its thresholds, or switch it on or off."""
    return services.price_alerts.update(principal, alert_id, body)


@router.delete(
    "/{alert_id}",
    status_code=204,
    response_class=Response,
    operation_id="deletePriceAlert",
    dependencies=needs(Permission.NOTIFICATIONS_MANAGE),
)
def delete_price_alert(
    alert_id: AlertId, services: ServicesDep, principal: PrincipalDep
) -> Response:
    services.price_alerts.delete(principal, alert_id)
    return Response(status_code=204)
