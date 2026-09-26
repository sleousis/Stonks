from __future__ import annotations

from fastapi import APIRouter

from stonks.api.deps import ServicesDep
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.config import RiskPolicy

router = APIRouter(prefix="/api/risk", tags=["risk"], responses=PROBLEM_RESPONSES)


@router.get("/policy", response_model=RiskPolicy, operation_id="getRiskPolicy")
def get_risk_policy(services: ServicesDep) -> RiskPolicy:
    """The ``[production.risk]`` limits applied between a strategy's orders
    and the broker."""
    return services.operations.risk_policy()
