from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from stonks.api.deps import PageDep, PortfolioIdDep, ServicesDep
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.pagination import Page
from stonks.app.risk_monitor import RiskMonitorService, RiskSnapshotView, RiskSummaryView
from stonks.config import RiskPolicy

router = APIRouter(prefix="/api/risk", tags=["risk"], responses=PROBLEM_RESPONSES)


def get_risk_monitor(services: ServicesDep) -> RiskMonitorService:
    return RiskMonitorService(services.context)


RiskMonitorDep = Annotated[RiskMonitorService, Depends(get_risk_monitor)]


@router.get("/policy", response_model=RiskPolicy, operation_id="getRiskPolicy")
def get_risk_policy(services: ServicesDep) -> RiskPolicy:
    """The ``[production.risk]`` limits applied between a strategy's orders
    and the broker."""
    return services.operations.risk_policy()


@router.get("/live", response_model=RiskSummaryView, operation_id="getLiveRisk")
def get_live_risk(monitor: RiskMonitorDep, portfolio_id: PortfolioIdDep) -> RiskSummaryView:
    """One of your portfolios on its latest tick day: one-day 95% and 99%
    VaR and expected shortfall, the rolling violation ratio and Kupiec test,
    and per strategy sleeve the same plus the alpha-decay check (BL-47)."""
    return monitor.latest(portfolio_id)


@router.get("/snapshots", response_model=Page[RiskSnapshotView], operation_id="listRiskSnapshots")
def list_risk_snapshots(
    monitor: RiskMonitorDep,
    page: PageDep,
    portfolio_id: PortfolioIdDep,
    strategy_id: Annotated[
        str | None,
        Query(max_length=200, description="one strategy's sleeve; default the whole portfolio"),
    ] = None,
    since: Annotated[date | None, Query(description="days on or after this one")] = None,
) -> Page[RiskSnapshotView]:
    """Daily risk snapshots of one of your portfolios, newest first."""
    return monitor.history(
        portfolio_id,
        strategy_id=strategy_id,
        since=since,
        limit=page.limit,
        offset=page.offset,
    )
