"""A live portfolio's owner settings (roadmap 19.6, 19.7): the allocation
Stonks may trade and the account profile that picks the account rules.
Changes need a fresh second factor (``live.manage``) and are audited."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path

from stonks.api.deps import PrincipalDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.live import (
    AccountProfileBody,
    AccountProfileView,
    LiveAllocationUpdate,
    LiveAllocationView,
    LiveRulesView,
    LiveService,
)
from stonks.auth import Permission

router = APIRouter(prefix="/api/portfolios", tags=["live"], responses=PROBLEM_RESPONSES)

PortfolioId = Annotated[str, Path(max_length=64)]


def _service(services: ServicesDep) -> LiveService:
    return LiveService(services.context)


@router.get(
    "/{portfolio_id}/live/allocation",
    response_model=LiveAllocationView,
    operation_id="getLiveAllocation",
)
def get_live_allocation(
    portfolio_id: PortfolioId, services: ServicesDep, principal: PrincipalDep
) -> LiveAllocationView:
    """How much Stonks may trade in this live portfolio. ``amount`` is
    ``null`` until you set it, and then nothing opens."""
    return _service(services).get_allocation(principal, portfolio_id)


@router.put(
    "/{portfolio_id}/live/allocation",
    response_model=LiveAllocationView,
    operation_id="setLiveAllocation",
    dependencies=needs(Permission.LIVE_MANAGE),
)
def set_live_allocation(
    portfolio_id: PortfolioId,
    body: LiveAllocationUpdate,
    services: ServicesDep,
    principal: PrincipalDep,
) -> LiveAllocationView:
    """Set the amount by hand, with a reason. Needs a fresh second factor.
    The book's gross exposure is capped at it (``capital_ramp``)."""
    return _service(services).set_allocation(principal, portfolio_id, body)


@router.get(
    "/{portfolio_id}/live/account-profile",
    response_model=AccountProfileView,
    operation_id="getAccountProfile",
)
def get_account_profile(
    portfolio_id: PortfolioId, services: ServicesDep, principal: PrincipalDep
) -> AccountProfileView:
    """The account profile (404 until one is set)."""
    return _service(services).get_profile(principal, portfolio_id)


@router.put(
    "/{portfolio_id}/live/account-profile",
    response_model=AccountProfileView,
    operation_id="setAccountProfile",
    dependencies=needs(Permission.LIVE_MANAGE),
)
def set_account_profile(
    portfolio_id: PortfolioId,
    body: AccountProfileBody,
    services: ServicesDep,
    principal: PrincipalDep,
) -> AccountProfileView:
    """Set where the account is held and its type. Shorts need a margin
    account. Needs a fresh second factor."""
    return _service(services).set_profile(principal, portfolio_id, body)


@router.get(
    "/{portfolio_id}/live/rules",
    response_model=LiveRulesView,
    operation_id="getLiveRules",
    dependencies=needs(Permission.READ),
)
def get_live_rules(
    portfolio_id: PortfolioId, services: ServicesDep, principal: PrincipalDep
) -> LiveRulesView:
    """Which live safeguards and account rules act on this portfolio, as its
    book follows them. Read only: the limits are set by the admin and your
    own risk limits."""
    return _service(services).rules(principal, portfolio_id)
