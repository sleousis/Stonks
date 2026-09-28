"""The demo portfolio (roadmap 23.17): sample data a new person can open,
clearly labelled, removable, and never mixed with real books or the lake."""

from __future__ import annotations

from fastapi import APIRouter, Response

from stonks.api.deps import PrincipalDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.demo import DemoPortfolioView, DemoService
from stonks.auth import Permission

router = APIRouter(prefix="/api/demo", tags=["demo"], responses=PROBLEM_RESPONSES)


def _service(services: ServicesDep) -> DemoService:
    found = getattr(services, "demo", None)
    return found if isinstance(found, DemoService) else DemoService(services.context)


@router.get("", response_model=DemoPortfolioView, operation_id="getDemoPortfolio")
def get_demo(services: ServicesDep, principal: PrincipalDep) -> DemoPortfolioView:
    """Your demo portfolio, or ``exists: false`` when you have not opened one."""
    return _service(services).get(principal)


@router.post(
    "",
    response_model=DemoPortfolioView,
    operation_id="openDemoPortfolio",
    dependencies=needs(Permission.READ),
)
def open_demo(services: ServicesDep, principal: PrincipalDep) -> DemoPortfolioView:
    """Open your demo portfolio: sample holdings and a year of made-up
    prices. Opening it again shows the same one."""
    return _service(services).open(principal)


@router.delete(
    "",
    status_code=204,
    response_class=Response,
    operation_id="removeDemoPortfolio",
    dependencies=needs(Permission.READ),
)
def remove_demo(services: ServicesDep, principal: PrincipalDep) -> Response:
    """Remove your demo portfolio. Nothing real changes."""
    _service(services).remove(principal)
    return Response(status_code=204)
