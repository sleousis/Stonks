"""Your portfolios and your subscriptions (roadmap step S7).

Everything here is about the caller: another user's portfolio or
subscription is a 404, admins included. Holdings live under
``/api/portfolio``."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path

from stonks.api.deps import PrincipalDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.portfolio import PortfolioSummaryView, TradingModeView
from stonks.app.subscriptions import SubscribeRequest, SubscriptionUpdate, SubscriptionView
from stonks.auth import Permission

router = APIRouter(prefix="/api/portfolios", tags=["portfolio"], responses=PROBLEM_RESPONSES)
subscriptions_router = APIRouter(
    prefix="/api/subscriptions", tags=["subscriptions"], responses=PROBLEM_RESPONSES
)

SubscriptionId = Annotated[str, Path(max_length=64)]


@router.get(
    "",
    response_model=list[PortfolioSummaryView],
    operation_id="listPortfolios",
    dependencies=needs(Permission.READ),
)
def list_portfolios(services: ServicesDep, principal: PrincipalDep) -> list[PortfolioSummaryView]:
    """Your portfolios, oldest first, each marked paper or live."""
    return services.portfolio.list_mine(principal)


@router.get(
    "/trading-modes",
    response_model=list[TradingModeView],
    operation_id="listTradingModes",
    dependencies=needs(Permission.READ),
)
def list_trading_modes(services: ServicesDep, principal: PrincipalDep) -> list[TradingModeView]:
    """For each of your portfolios: does it trade paper or live money, and
    through which broker."""
    return services.portfolio.trading_modes(principal)


@subscriptions_router.get(
    "",
    response_model=list[SubscriptionView],
    operation_id="listSubscriptions",
    dependencies=needs(Permission.READ),
)
def list_subscriptions(services: ServicesDep, principal: PrincipalDep) -> list[SubscriptionView]:
    """Your subscriptions, with the paper-day count and what still blocks auto."""
    return services.subscriptions.list(principal)


@subscriptions_router.post(
    "",
    status_code=201,
    response_model=SubscriptionView,
    operation_id="subscribe",
    dependencies=needs(Permission.PORTFOLIO_TRADE),
)
def subscribe(
    body: SubscribeRequest, services: ServicesDep, principal: PrincipalDep
) -> SubscriptionView:
    """Follow a strategy: ``notify`` (signals only) or ``paper`` on one of
    your portfolios. ``auto`` is never the starting mode (409)."""
    return services.subscriptions.subscribe(principal, body)


@subscriptions_router.patch(
    "/{subscription_id}",
    response_model=SubscriptionView,
    operation_id="updateSubscription",
    dependencies=needs(Permission.PORTFOLIO_TRADE),
)
def update_subscription(
    subscription_id: SubscriptionId,
    body: SubscriptionUpdate,
    services: ServicesDep,
    principal: PrincipalDep,
) -> SubscriptionView:
    """Turn a subscription on or off, or change its mode. Switching to
    ``auto`` needs a second factor from the last few minutes (403
    ``step_up_required``) and a passing checklist, including 20 paper
    trading days (409 ``auto_blocked`` with ``blockers``)."""
    return services.subscriptions.update(principal, subscription_id, body)
