from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query

from stonks.api.deps import PageDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.market import (
    DEFAULT_BAR_LIMIT,
    MAX_BAR_LIMIT,
    BarSeries,
    BreadthView,
    CoverageRow,
    InstrumentView,
)
from stonks.app.pagination import Page
from stonks.auth import Permission
from stonks.core.types import AssetClass

router = APIRouter(prefix="/api/market", tags=["market"], responses=PROBLEM_RESPONSES)


@router.get("/instruments", response_model=Page[InstrumentView], operation_id="listInstruments")
def list_instruments(
    services: ServicesDep,
    page: PageDep,
    q: Annotated[str | None, Query(max_length=100, description="id or name substring")] = None,
    asset_class: AssetClass | None = None,
) -> Page[InstrumentView]:
    return services.market.instruments(
        q=q, asset_class=asset_class, limit=page.limit, offset=page.offset
    )


@router.get("/bars", response_model=BarSeries, operation_id="getBars")
def get_bars(
    services: ServicesDep,
    ticker: str,
    interval: str = "1d",
    start: date | None = None,
    end: date | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_BAR_LIMIT)] = DEFAULT_BAR_LIMIT,
) -> BarSeries:
    """Bars in ``[start, end]``; the most recent ``limit`` when truncated."""
    return services.market.bars(ticker, interval=interval, start=start, end=end, limit=limit)


@router.get("/coverage", response_model=Page[CoverageRow], operation_id="listCoverage")
def list_coverage(
    services: ServicesDep,
    page: PageDep,
    ticker: str | None = None,
    interval: str | None = None,
) -> Page[CoverageRow]:
    """First/last bar and row count per (ticker, interval)."""
    return services.market.coverage(
        ticker=ticker, interval=interval, limit=page.limit, offset=page.offset
    )


@router.get(
    "/breadth",
    response_model=BreadthView,
    operation_id="getMarketBreadth",
    dependencies=needs(Permission.READ),
)
def get_breadth(
    services: ServicesDep,
    as_of: Annotated[
        date | None, Query(description="Measure on this day. Default: the last day with bars.")
    ] = None,
) -> BreadthView:
    """How many stocks take part in the market's move: advances and
    declines, the share above the 50 and 200 day averages, new one year
    highs and lows, and distribution days on the index, with plain words.
    Display only."""
    return services.market.breadth(as_of=as_of)
