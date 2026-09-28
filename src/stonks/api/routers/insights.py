"""Portfolio insights (roadmap 15.4): allocation, exposure, P&L over periods,
risk and strategy agreement for one of your portfolios, synced broker
accounts included. ``portfolio_id`` must be yours (404 otherwise, admins
included); admins get totals across every book, never holdings."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from stonks.api.deps import PortfolioIdDep, PrincipalDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.insights import AgreementView, InsightsTotalsView, InsightsView, LookThroughView
from stonks.auth import Permission

router = APIRouter(prefix="/api/insights", tags=["insights"], responses=PROBLEM_RESPONSES)


@router.get("", response_model=InsightsView, operation_id="getInsights")
def get_insights(
    services: ServicesDep,
    portfolio_id: PortfolioIdDep,
    benchmark: Annotated[
        str | None,
        Query(
            max_length=32,
            pattern=r"^[A-Za-z0-9_.\-]+$",
            description="Ticker beta is measured against. Default SPY.US.",
        ),
    ] = None,
) -> InsightsView:
    """Allocation (asset class, sector, currency, ticker), exposure (gross,
    net, beta), P&L over periods and risk (volatility, drawdown, VaR,
    concentration) of one of your portfolios."""
    return services.insights.insights(portfolio_id, benchmark=benchmark)


@router.get("/agreement", response_model=AgreementView, operation_id="getStrategyAgreement")
def get_agreement(services: ServicesDep, portfolio_id: PortfolioIdDep) -> AgreementView:
    """For each holding, whether every active strategy's latest signal
    agrees or disagrees with it, and why."""
    return services.insights.agreement(portfolio_id)


@router.get(
    "/look-through",
    response_model=LookThroughView,
    operation_id="getLookThrough",
    dependencies=needs(Permission.READ),
)
def get_look_through(
    services: ServicesDep,
    portfolio_id: PortfolioIdDep,
    top: Annotated[int, Query(ge=1, le=100, description="Single names to list.")] = 20,
) -> LookThroughView:
    """Exposure by sector, country and single name with each held fund
    (an ETF) split into what it holds, from the latest holdings lists. Your
    real weight in Apple counts AAPL plus its share of SPY and QQQ."""
    return services.insights.look_through(portfolio_id, top=top)


@router.get(
    "/totals",
    response_model=InsightsTotalsView,
    operation_id="getInsightsTotals",
    dependencies=needs(Permission.PORTFOLIO_TOTALS),
)
def get_totals(services: ServicesDep, principal: PrincipalDep) -> InsightsTotalsView:
    """Admins: asset-class allocation and exposure summed over every active
    portfolio. No tickers and no per-person numbers."""
    return services.insights.totals(principal)
