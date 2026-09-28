"""Manual orders (roadmap 20.1): place, preview, change and cancel your own
orders. Each goes through the kill switch, every halt and every risk rule
of the book. A book that trades real money needs a fresh second factor
(403 ``step_up_required`` otherwise). A refused order is 409
``order_refused`` with the risk rules' adjustments."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path

from stonks.api.deps import PrincipalDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.manual_orders import (
    ManualOrderChange,
    ManualOrderRequest,
    ManualOrderResult,
    OrderCancelRequest,
    OrderCancelResult,
    TradePlanRequest,
    TradePlanView,
)
from stonks.auth import Permission

router = APIRouter(prefix="/api/orders", tags=["orders"], responses=PROBLEM_RESPONSES)

ClientId = Annotated[str, Path(max_length=200, description="The order's client id")]


@router.post(
    "/manual",
    response_model=ManualOrderResult,
    operation_id="placeManualOrder",
    dependencies=needs(Permission.PORTFOLIO_TRADE),
)
def place_manual_order(
    services: ServicesDep, principal: PrincipalDep, body: ManualOrderRequest
) -> ManualOrderResult:
    """Place an order on one of your portfolios. A simulated book fills at
    once at the latest close. A book at a broker sends it through the broker.
    The same ``client_id`` places the order once."""
    return services.manual_orders.place(principal, body)


@router.post(
    "/manual/preview",
    response_model=ManualOrderResult,
    operation_id="previewManualOrder",
    dependencies=needs(Permission.PORTFOLIO_TRADE),
)
def preview_manual_order(
    services: ServicesDep, principal: PrincipalDep, body: ManualOrderRequest
) -> ManualOrderResult:
    """Run every check of an order and say what would be placed. Nothing is
    recorded or sent."""
    return services.manual_orders.preview(principal, body)


@router.post(
    "/manual/plan",
    response_model=TradePlanView,
    operation_id="planManualOrder",
    dependencies=needs(Permission.PORTFOLIO_TRADE),
)
def plan_manual_order(
    services: ServicesDep, principal: PrincipalDep, body: TradePlanRequest
) -> TradePlanView:
    """Size an entry from the risk you choose (a percent of the book or an
    amount) and the distance to your stop, in whole shares within the cash.
    Nothing is placed."""
    return services.manual_orders.plan(principal, body)


@router.post(
    "/{client_id}/change",
    response_model=ManualOrderResult,
    operation_id="changeManualOrder",
    dependencies=needs(Permission.PORTFOLIO_TRADE),
)
def change_manual_order(
    services: ServicesDep, principal: PrincipalDep, client_id: ClientId, body: ManualOrderChange
) -> ManualOrderResult:
    """Change the quantity or limit of one of your working manual orders:
    it is cancelled at the broker and a new order replaces it."""
    return services.manual_orders.change(principal, client_id, body)


@router.post(
    "/{client_id}/cancel",
    response_model=OrderCancelResult,
    operation_id="cancelOrder",
    dependencies=needs(Permission.PORTFOLIO_TRADE),
)
def cancel_order(
    services: ServicesDep, principal: PrincipalDep, client_id: ClientId, body: OrderCancelRequest
) -> OrderCancelResult:
    """Cancel one working order of your portfolio, yours or a strategy's."""
    return services.manual_orders.cancel(principal, client_id, body)
