from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query

from stonks.api.deps import PageDep, PrincipalDep, ServicesDep, require_permission
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.pagination import Page
from stonks.app.strategies import (
    StatusChangeRequest,
    StatusChangeView,
    StrategyDetail,
    StrategyStatus,
    StrategyStatusCounts,
    StrategySummary,
)
from stonks.auth import Permission, Principal

router = APIRouter(prefix="/api/strategies", tags=["strategies"], responses=PROBLEM_RESPONSES)

#: Optional body: the routes answer 409 (promotion refused by the go-live
#: gate) or 422 (missing reason, short override reason) without one.
StatusBody = Annotated[StatusChangeRequest | None, Body()]
_promote = [Depends(require_permission(Permission.STRATEGY_PROMOTE))]


@router.get("", response_model=Page[StrategySummary], operation_id="listStrategies")
def list_strategies(
    services: ServicesDep,
    page: PageDep,
    status: StrategyStatus | None = None,
    q: Annotated[
        str | None,
        Query(max_length=200, description="case-insensitive substring of the id or class path"),
    ] = None,
) -> Page[StrategySummary]:
    return services.strategies.list(status=status, q=q, limit=page.limit, offset=page.offset)


@router.get("/summary", response_model=StrategyStatusCounts, operation_id="getStrategySummary")
def strategy_summary(services: ServicesDep) -> StrategyStatusCounts:
    """Registered strategies counted by lifecycle status, in one call."""
    return services.strategies.counts()


@router.get("/{strategy_id}", response_model=StrategyDetail, operation_id="getStrategy")
def get_strategy(strategy_id: str, services: ServicesDep) -> StrategyDetail:
    return services.strategies.get(strategy_id)


@router.get(
    "/{strategy_id}/history",
    response_model=list[StatusChangeView],
    operation_id="getStrategyHistory",
)
def get_strategy_history(strategy_id: str, services: ServicesDep) -> list[StatusChangeView]:
    """The strategy's audited status changes and interventions, oldest first."""
    return services.strategies.history(strategy_id)


def _change(
    services,
    principal: Principal,
    strategy_id: str,
    status: StrategyStatus,
    body: StatusChangeRequest | None,
):
    """The audit actor is the caller (``user:<id>``); ``body.actor`` is
    accepted for old clients and ignored."""
    body = body or StatusChangeRequest()
    return services.strategies.set_status(
        strategy_id,
        status,
        actor=principal.actor,
        reason=body.reason,
        override=body.override if status == "active" else False,
    )


@router.post(
    "/{strategy_id}/promote",
    response_model=StrategyDetail,
    operation_id="promoteStrategy",
    dependencies=_promote,
)
def promote(
    strategy_id: str, services: ServicesDep, principal: PrincipalDep, body: StatusBody = None
) -> StrategyDetail:
    """Move to ``active``. Needs a passing go-live check (409 with the
    failing checks otherwise), or ``override`` with a ``reason`` of at
    least 20 characters (422 when shorter)."""
    return _change(services, principal, strategy_id, "active", body)


@router.post(
    "/{strategy_id}/retire",
    response_model=StrategyDetail,
    operation_id="retireStrategy",
    dependencies=_promote,
)
def retire(
    strategy_id: str, services: ServicesDep, principal: PrincipalDep, body: StatusBody = None
) -> StrategyDetail:
    """Move to ``retired``; needs a ``reason`` (422 without one)."""
    return _change(services, principal, strategy_id, "retired", body)


@router.post(
    "/{strategy_id}/shadow",
    response_model=StrategyDetail,
    operation_id="shadowStrategy",
    dependencies=_promote,
)
def shadow(
    strategy_id: str, services: ServicesDep, principal: PrincipalDep, body: StatusBody = None
) -> StrategyDetail:
    """Move to ``shadow``; needs a ``reason`` (422 without one)."""
    return _change(services, principal, strategy_id, "shadow", body)
