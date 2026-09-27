"""Price charts (roadmap 13.5): bars, your fills and the strategies'
signals of one ticker in one read."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Path, Query

from stonks.api.deps import OptionalPrincipalDep, ServicesDep
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.charts import DEFAULT_CHART_BARS, MAX_CHART_BARS, ChartService, ChartView
from stonks.app.errors import NotFoundError

router = APIRouter(prefix="/api/charts", tags=["charts"], responses=PROBLEM_RESPONSES)


@router.get("/{ticker}", response_model=ChartView, operation_id="getChart")
def get_chart(
    ticker: Annotated[str, Path(max_length=40, description="instrument id, e.g. AAPL.US")],
    services: ServicesDep,
    principal: OptionalPrincipalDep,
    interval: str = "1d",
    start: date | None = None,
    end: date | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_CHART_BARS)] = DEFAULT_CHART_BARS,
    portfolio_id: Annotated[
        str | None,
        Query(max_length=64, description="One of your portfolios (404 otherwise)."),
    ] = None,
    strategy_id: Annotated[
        str | None, Query(max_length=200, description="only this strategy's signals")
    ] = None,
) -> ChartView:
    """Bars of one ticker (the latest ``limit`` in the window), your fills
    of it in one of your portfolios (default your own book; none when you
    have no portfolio) and every strategy's signal events for it."""
    resolved: str | None = None
    if principal is not None:
        try:
            resolved = services.portfolio.resolve(principal, portfolio_id)
        except NotFoundError:
            if portfolio_id is not None:
                raise
    return ChartService(services.context).chart(
        ticker,
        portfolio_id=resolved,
        interval=interval,
        start=start,
        end=end,
        limit=limit,
        strategy_id=strategy_id,
    )
