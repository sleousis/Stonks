from __future__ import annotations

from fastapi import APIRouter

from stonks.api.deps import PageDep, ServicesDep
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.pagination import Page
from stonks.app.portfolio import PortfolioView, SnapshotView

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"], responses=PROBLEM_RESPONSES)


@router.get("", response_model=PortfolioView, operation_id="getPortfolio")
def get_portfolio(services: ServicesDep) -> PortfolioView:
    """Latest snapshot, positions valued at the latest stored closes."""
    return services.portfolio.current()


@router.get("/snapshots", response_model=Page[SnapshotView], operation_id="listPortfolioSnapshots")
def list_snapshots(services: ServicesDep, page: PageDep) -> Page[SnapshotView]:
    return services.portfolio.snapshots(limit=page.limit, offset=page.offset)
