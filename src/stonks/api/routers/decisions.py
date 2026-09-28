"""Why did or didn't we trade (roadmap 23.7)."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query

from stonks.api.deps import PageDep, PortfolioIdDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.decisions import DecisionService, TradeDecisionView
from stonks.app.pagination import Page
from stonks.auth import Permission

router = APIRouter(prefix="/api/decisions", tags=["portfolio"], responses=PROBLEM_RESPONSES)


@router.get(
    "",
    response_model=Page[TradeDecisionView],
    operation_id="listTradeDecisions",
    dependencies=needs(Permission.READ),
)
def list_trade_decisions(
    services: ServicesDep,
    page: PageDep,
    portfolio_id: PortfolioIdDep,
    ticker: Annotated[
        str | None, Query(max_length=64, description="one ticker, e.g. AAPL.US")
    ] = None,
    strategy_id: Annotated[
        str | None, Query(max_length=200, description="rows this strategy owned or scored")
    ] = None,
    tick_id: Annotated[str | None, Query(max_length=64, description="one tick")] = None,
    since: Annotated[date | None, Query(description="days on or after this one")] = None,
) -> Page[TradeDecisionView]:
    """Why a ticker did or did not trade in one of your portfolios, newest
    first: the step that kept it out or trimmed it (universe, rank,
    constructor, buffer, stale price, a named risk rule, a halt)."""
    return DecisionService(services.context).list(
        portfolio_id,
        ticker=ticker,
        strategy_id=strategy_id,
        tick_id=tick_id,
        since=since,
        limit=page.limit,
        offset=page.offset,
    )
