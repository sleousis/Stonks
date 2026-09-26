from __future__ import annotations

from fastapi import APIRouter

from stonks.api.deps import ServicesDep
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.catalog import IntervalInfo, StrategyClassInfo

router = APIRouter(prefix="/api/catalog", tags=["catalog"], responses=PROBLEM_RESPONSES)


@router.get(
    "/strategies", response_model=list[StrategyClassInfo], operation_id="listStrategyClasses"
)
def list_strategy_classes(services: ServicesDep) -> list[StrategyClassInfo]:
    return services.catalog.strategies()


@router.get("/intervals", response_model=list[IntervalInfo], operation_id="listIntervals")
def list_intervals(services: ServicesDep) -> list[IntervalInfo]:
    return services.catalog.intervals()


@router.get("/asset-classes", response_model=list[str], operation_id="listAssetClasses")
def list_asset_classes(services: ServicesDep) -> list[str]:
    return services.catalog.asset_classes()
