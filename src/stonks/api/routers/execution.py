"""Execution algos and the rebalancing planner (roadmap 23.16).

A portfolio's orders go out as plain limit orders unless you set an algo:
Adaptive, VWAP or TWAP, run by IBKR itself or sent as child slices by
Stonks at other brokers. The planner shows the trades that reach target
weights, with costs, turnover and a tax preview, and sends nothing.
Confirming a plan writes order tickets that wait for your approval."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, Response

from stonks.api.deps import PrincipalDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.planner import (
    AlgoList,
    AlgoSettingList,
    AlgoSettingUpdate,
    AlgoSettingView,
    ParentOrderList,
    PlanConfirm,
    PlanConfirmResult,
    PlanRequest,
    RebalancePlanView,
)
from stonks.auth import Permission

router = APIRouter(tags=["execution"], responses=PROBLEM_RESPONSES)

PortfolioId = Annotated[str, Path(max_length=64)]


@router.get("/api/execution/algos", response_model=AlgoList, operation_id="listExecutionAlgos")
def list_execution_algos(services: ServicesDep, principal: PrincipalDep) -> AlgoList:
    """Every execution algo: its parameters, defaults and the costs the
    backtest assumes for it."""
    return services.execution.algos(principal)


@router.get(
    "/api/portfolios/{portfolio_id}/execution-algos",
    response_model=AlgoSettingList,
    operation_id="listExecutionAlgoSettings",
)
def list_execution_algo_settings(
    portfolio_id: PortfolioId, services: ServicesDep, principal: PrincipalDep
) -> AlgoSettingList:
    """The portfolio's algo setting and each strategy's override. Empty:
    plain orders."""
    return services.execution.settings(principal, portfolio_id)


@router.put(
    "/api/portfolios/{portfolio_id}/execution-algos",
    response_model=AlgoSettingView,
    operation_id="setExecutionAlgo",
    dependencies=needs(Permission.PORTFOLIO_MANAGE),
)
def set_execution_algo(
    portfolio_id: PortfolioId,
    body: AlgoSettingUpdate,
    services: ServicesDep,
    principal: PrincipalDep,
) -> AlgoSettingView:
    """Work the portfolio's orders (or one strategy's, with strategy_id)
    with an algo. Stops, options and one-cancels-other orders stay plain."""
    return services.execution.set_algo(principal, portfolio_id, body)


@router.delete(
    "/api/portfolios/{portfolio_id}/execution-algos",
    status_code=204,
    response_class=Response,
    operation_id="clearExecutionAlgo",
    dependencies=needs(Permission.PORTFOLIO_MANAGE),
)
def clear_execution_algo(
    portfolio_id: PortfolioId,
    services: ServicesDep,
    principal: PrincipalDep,
    strategy_id: Annotated[str | None, Query(max_length=64)] = None,
) -> Response:
    """Back to plain orders, or a strategy back to the portfolio's setting."""
    services.execution.clear_algo(principal, portfolio_id, strategy_id)
    return Response(status_code=204)


@router.get(
    "/api/portfolios/{portfolio_id}/algo-parents",
    response_model=ParentOrderList,
    operation_id="listAlgoParents",
)
def list_algo_parents(
    portfolio_id: PortfolioId, services: ServicesDep, principal: PrincipalDep
) -> ParentOrderList:
    """Parent orders Stonks works as child slices, with each slice."""
    return services.execution.parents(principal, portfolio_id)


@router.post(
    "/api/planner/plan",
    response_model=RebalancePlanView,
    operation_id="planRebalance",
    dependencies=needs(Permission.READ),
)
def plan_rebalance(
    body: PlanRequest, services: ServicesDep, principal: PrincipalDep
) -> RebalancePlanView:
    """The trades that move a portfolio to a strategy's model weights or to
    your own list: whole shares, costs, turnover and a tax preview. Nothing
    is written or sent."""
    return services.execution.plan(principal, body)


@router.post(
    "/api/planner/confirm",
    response_model=PlanConfirmResult,
    operation_id="confirmRebalance",
    dependencies=needs(Permission.PORTFOLIO_TRADE),
)
def confirm_rebalance(
    body: PlanConfirm, services: ServicesDep, principal: PrincipalDep
) -> PlanConfirmResult:
    """One order ticket per trade of the plan. Each waits for your approval
    (a fresh second factor) before it is sent in the submit window."""
    return services.execution.confirm(principal, body)
