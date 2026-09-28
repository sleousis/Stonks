"""The starter set (complexity audit F19): three simple strategies On trial
and a small trading universe, so a fresh install is never empty."""

from __future__ import annotations

from fastapi import APIRouter

from stonks.api.deps import PrincipalDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.starter import StarterInstallView, StarterService, StarterView
from stonks.auth import Permission

router = APIRouter(prefix="/api/starter", tags=["starter"], responses=PROBLEM_RESPONSES)


@router.get("", response_model=StarterView, operation_id="getStarterSet")
def get_starter_set(services: ServicesDep, principal: PrincipalDep) -> StarterView:
    """The starter strategies, whether each is registered, and the starter
    trading universe."""
    return StarterService(services.context).status(principal)


@router.post(
    "/install",
    response_model=StarterInstallView,
    operation_id="installStarterSet",
    dependencies=needs(Permission.OPERATIONS_RUN),
)
def install_starter_set(services: ServicesDep, principal: PrincipalDep) -> StarterInstallView:
    """Register the missing starters On trial (never approved: they go
    through the go-live check like any strategy) and set the trading
    universe when none is configured. Safe to call again."""
    return StarterService(services.context).install(principal)
