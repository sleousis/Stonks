from __future__ import annotations

from fastapi import APIRouter

from stonks.api.deps import PageDep, ServicesDep
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.orders import FillView, OrderView
from stonks.app.pagination import Page

router = APIRouter(prefix="/api/orders", tags=["orders"], responses=PROBLEM_RESPONSES)


@router.get("", response_model=Page[OrderView], operation_id="listOrders")
def list_orders(
    services: ServicesDep,
    page: PageDep,
    tick_id: str | None = None,
    strategy_id: str | None = None,
    ticker: str | None = None,
    status: str | None = None,
) -> Page[OrderView]:
    return services.orders.orders(
        tick_id=tick_id,
        strategy_id=strategy_id,
        ticker=ticker,
        status=status,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/fills", response_model=Page[FillView], operation_id="listFills")
def list_fills(
    services: ServicesDep,
    page: PageDep,
    tick_id: str | None = None,
    ticker: str | None = None,
    order_client_id: str | None = None,
) -> Page[FillView]:
    return services.orders.fills(
        tick_id=tick_id,
        ticker=ticker,
        order_client_id=order_client_id,
        limit=page.limit,
        offset=page.offset,
    )
