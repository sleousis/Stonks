"""Your portfolio: the book, its snapshots and, for admins, totals across
every trader. Reads take an optional ``portfolio_id`` that must be yours
(404 otherwise); holdings always need a credential, even on loopback."""

from __future__ import annotations

from fastapi import APIRouter

from stonks.api.deps import PageDep, PortfolioIdDep, PrincipalDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.pagination import Page
from stonks.app.portfolio import PortfolioTotalsView, PortfolioView, SnapshotView
from stonks.auth import Permission

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"], responses=PROBLEM_RESPONSES)


@router.get("", response_model=PortfolioView, operation_id="getPortfolio")
def get_portfolio(services: ServicesDep, portfolio_id: PortfolioIdDep) -> PortfolioView:
    """Latest snapshot, positions valued at the latest stored closes."""
    return services.portfolio.current(portfolio_id)


@router.get("/snapshots", response_model=Page[SnapshotView], operation_id="listPortfolioSnapshots")
def list_snapshots(
    services: ServicesDep, page: PageDep, portfolio_id: PortfolioIdDep
) -> Page[SnapshotView]:
    return services.portfolio.snapshots(
        limit=page.limit, offset=page.offset, portfolio_id=portfolio_id
    )


@router.get(
    "/totals",
    response_model=PortfolioTotalsView,
    operation_id="getPortfolioTotals",
    dependencies=needs(Permission.PORTFOLIO_TOTALS),
)
def get_totals(services: ServicesDep, principal: PrincipalDep) -> PortfolioTotalsView:
    """Admins: cash and value summed across every active portfolio. No
    tickers and no per-person numbers."""
    return services.portfolio.totals(principal)
