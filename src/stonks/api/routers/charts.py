"""Price charts (roadmap 13.5): bars, your fills and the strategies'
signals of one ticker in one read, and several tickers compared on one
scale with drawdown and rolling Sharpe."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Path, Query

from stonks.api.deps import OptionalPrincipalDep, ServicesDep
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.charts import (
    DEFAULT_CHART_BARS,
    DEFAULT_ROLLING_WINDOW,
    MAX_CHART_BARS,
    MAX_ROLLING_WINDOW,
    MIN_ROLLING_WINDOW,
    ChartService,
    ChartView,
    CompareView,
)
from stonks.app.errors import NotFoundError

router = APIRouter(prefix="/api/charts", tags=["charts"], responses=PROBLEM_RESPONSES)


# Declared before ``/{ticker}`` so "compare" is never read as a ticker.
@router.get("/compare", response_model=CompareView, operation_id="compareCharts")
def compare_charts(
    services: ServicesDep,
    tickers: Annotated[
        str,
        Query(
            min_length=1,
            max_length=400,
            description="comma-separated instrument ids, at most 6, e.g. AAPL.US,MSFT.US",
        ),
    ],
    start: date | None = None,
    end: date | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_CHART_BARS)] = DEFAULT_CHART_BARS,
    window: Annotated[
        int,
        Query(
            ge=MIN_ROLLING_WINDOW,
            le=MAX_ROLLING_WINDOW,
            description="bars behind each rolling Sharpe point",
        ),
    ] = DEFAULT_ROLLING_WINDOW,
) -> CompareView:
    """Daily adjusted closes of up to six tickers rebased to 100 on the
    first day they all have a price, each with its drawdown from the
    running peak and its rolling Sharpe (annualized, 365 days for crypto).
    Tickers without prices are listed in ``missing``."""
    return ChartService(services.context).compare(
        tickers.split(","), start=start, end=end, limit=limit, window=window
    )


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
