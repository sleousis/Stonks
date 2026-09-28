"""Deposits and withdrawals of your portfolios (roadmap 20.5). Returns are
time and money weighted from them, so a deposit is never profit. Another
person's portfolio is a 404, admins included."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path

from stonks.api.deps import PageDep, PrincipalDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.cash_flows import CashFlowCreate, CashFlowService, CashFlowView
from stonks.app.pagination import Page, page_of
from stonks.auth import Permission

router = APIRouter(prefix="/api/portfolios", tags=["portfolio"], responses=PROBLEM_RESPONSES)

PortfolioId = Annotated[str, Path(max_length=64)]


def _service(services: ServicesDep) -> CashFlowService:
    return CashFlowService(services.context)


@router.get(
    "/{portfolio_id}/cash-flows",
    response_model=Page[CashFlowView],
    operation_id="listCashFlows",
)
def list_cash_flows(
    portfolio_id: PortfolioId, services: ServicesDep, principal: PrincipalDep, page: PageDep
) -> Page[CashFlowView]:
    """Every deposit and withdrawal of one of your portfolios, oldest first:
    recorded ones and those a broker sync brought in."""
    return page_of(_service(services).list(principal, portfolio_id), page)


@router.post(
    "/{portfolio_id}/cash-flows",
    status_code=201,
    response_model=CashFlowView,
    operation_id="recordCashFlow",
    dependencies=needs(Permission.PORTFOLIO_MANAGE),
)
def record_cash_flow(
    portfolio_id: PortfolioId, body: CashFlowCreate, services: ServicesDep, principal: PrincipalDep
) -> CashFlowView:
    """Record a deposit or a withdrawal on one of your simulated portfolios.
    It moves the book's cash. Broker books get theirs from the sync."""
    return _service(services).record(principal, portfolio_id, body)
