from __future__ import annotations

from datetime import date

from fastapi import APIRouter

from stonks.api.deps import ServicesDep
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.golive import GoLiveReport, golive_service

router = APIRouter(prefix="/api/strategies", tags=["strategies"], responses=PROBLEM_RESPONSES)


@router.get("/{strategy_id}/golive", response_model=GoLiveReport, operation_id="getGoLiveReport")
def get_golive(strategy_id: str, services: ServicesDep, since: date | None = None) -> GoLiveReport:
    """Every go-live check of the strategy's paper period against
    ``[golive]`` (same as ``stonks golive check``). Reports only; promotion
    stays a human action."""
    return golive_service(services).check(strategy_id, since=since)
