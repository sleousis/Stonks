from __future__ import annotations

from fastapi import APIRouter

from stonks.api.deps import PageDep, ServicesDep
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.pagination import Page
from stonks.app.strategies import (
    StrategyDetail,
    StrategyStatus,
    StrategyStatusCounts,
    StrategySummary,
)

router = APIRouter(prefix="/api/strategies", tags=["strategies"], responses=PROBLEM_RESPONSES)


@router.get("", response_model=Page[StrategySummary], operation_id="listStrategies")
def list_strategies(
    services: ServicesDep, page: PageDep, status: StrategyStatus | None = None
) -> Page[StrategySummary]:
    return services.strategies.list(status=status, limit=page.limit, offset=page.offset)


@router.get("/summary", response_model=StrategyStatusCounts, operation_id="getStrategySummary")
def strategy_summary(services: ServicesDep) -> StrategyStatusCounts:
    """Registered strategies counted by lifecycle status, in one call."""
    return services.strategies.counts()


@router.get("/{strategy_id}", response_model=StrategyDetail, operation_id="getStrategy")
def get_strategy(strategy_id: str, services: ServicesDep) -> StrategyDetail:
    return services.strategies.get(strategy_id)


@router.post(
    "/{strategy_id}/promote", response_model=StrategyDetail, operation_id="promoteStrategy"
)
def promote(strategy_id: str, services: ServicesDep) -> StrategyDetail:
    return services.strategies.promote(strategy_id)


@router.post("/{strategy_id}/retire", response_model=StrategyDetail, operation_id="retireStrategy")
def retire(strategy_id: str, services: ServicesDep) -> StrategyDetail:
    return services.strategies.retire(strategy_id)


@router.post("/{strategy_id}/shadow", response_model=StrategyDetail, operation_id="shadowStrategy")
def shadow(strategy_id: str, services: ServicesDep) -> StrategyDetail:
    return services.strategies.shadow(strategy_id)
