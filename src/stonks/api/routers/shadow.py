from __future__ import annotations

from datetime import date

from fastapi import APIRouter

from stonks.api.deps import PageDep, ServicesDep
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.operations import PnlSeries, ShadowDecisionView, ShadowPnlSummary
from stonks.app.pagination import Page

router = APIRouter(prefix="/api/shadow", tags=["shadow"], responses=PROBLEM_RESPONSES)


@router.get(
    "/decisions", response_model=Page[ShadowDecisionView], operation_id="listShadowDecisions"
)
def list_shadow_decisions(
    services: ServicesDep,
    page: PageDep,
    strategy_id: str | None = None,
    ticker: str | None = None,
    as_of: date | None = None,
) -> Page[ShadowDecisionView]:
    """Virtual orders shadow strategies placed, newest first."""
    return services.operations.shadow_decisions(
        strategy_id=strategy_id, ticker=ticker, as_of=as_of, limit=page.limit, offset=page.offset
    )


@router.get("/pnl", response_model=Page[ShadowPnlSummary], operation_id="listShadowPnl")
def list_shadow_pnl(
    services: ServicesDep, page: PageDep, since: date | None = None
) -> Page[ShadowPnlSummary]:
    """Latest value, return and worst drawdown of every shadow portfolio."""
    return services.operations.shadow_pnl_summaries(
        since=since, limit=page.limit, offset=page.offset
    )


@router.get("/strategies/{strategy_id}/pnl", response_model=PnlSeries, operation_id="getShadowPnl")
def get_shadow_pnl(strategy_id: str, services: ServicesDep, since: date | None = None) -> PnlSeries:
    """Daily P&L of one shadow strategy's virtual portfolio."""
    return services.operations.shadow_pnl(strategy_id, since=since)
