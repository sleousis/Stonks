"""Options research routes (roadmap 17.6): stored chains with Greeks, the
options strategies and structures, a structure's payoff, and options
backtest jobs. Research only: nothing here trades options."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Path, Response

from stonks.api.deps import OptionalPrincipalDep, PageDep, PrincipalDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.api.routers._jobs_common import JOB_CREATED, accepted
from stonks.app.jobs import Job
from stonks.app.options import (
    OPTIONS_BACKTEST_JOB,
    OptionChainView,
    OptionPayoffRequest,
    OptionPayoffView,
    OptionsBacktestRequest,
    OptionsBacktestView,
    OptionStrategyView,
    OptionStructureView,
    OptionUnderlyingView,
)
from stonks.app.pagination import Page, page_of
from stonks.auth import Permission

router = APIRouter(prefix="/api/options", tags=["options"], responses=PROBLEM_RESPONSES)

_Underlying = Annotated[str, Path(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9._\-]+$")]


@router.get(
    "/underlyings",
    response_model=Page[OptionUnderlyingView],
    operation_id="listOptionUnderlyings",
    dependencies=needs(Permission.READ),
)
def list_underlyings(services: ServicesDep, page: PageDep) -> Page[OptionUnderlyingView]:
    """Underlyings with stored option chains, and the days they cover."""
    return page_of(services.options.underlyings(), page)


@router.get(
    "/chains/{underlying}",
    response_model=OptionChainView,
    operation_id="getOptionChain",
    dependencies=needs(Permission.READ),
)
def get_chain(
    underlying: _Underlying,
    services: ServicesDep,
    as_of: date | None = None,
    expiry: date | None = None,
) -> OptionChainView:
    """One expiry of a stored chain with our implied vol and Greeks, calls
    and puts side by side by strike. ``as_of`` picks the last stored day on
    or before it (the latest without it). ``expiry`` defaults to the one
    nearest 30 days out."""
    return services.options.chain(underlying, as_of=as_of, expiry=expiry)


@router.get(
    "/strategies",
    response_model=list[OptionStrategyView],
    operation_id="listOptionStrategies",
    dependencies=needs(Permission.READ),
)
def list_strategies(services: ServicesDep) -> list[OptionStrategyView]:
    """The options strategy catalog with each hypothesis and parameters."""
    return services.options.strategies()


@router.get(
    "/structures",
    response_model=list[OptionStructureView],
    operation_id="listOptionStructures",
    dependencies=needs(Permission.READ),
)
def list_structures(services: ServicesDep) -> list[OptionStructureView]:
    """Structures a payoff can be drawn for, with the parameters each reads."""
    return services.options.structures()


@router.post(
    "/payoff",
    response_model=OptionPayoffView,
    operation_id="getOptionPayoff",
    dependencies=needs(Permission.READ),
)
def get_payoff(body: OptionPayoffRequest, services: ServicesDep) -> OptionPayoffView:
    """One unit of a structure picked from a stored chain by delta and days
    to expiry, and its profit at expiry over a range of underlying prices,
    with max loss, max gain and breakevens. A read sent as a POST."""
    return services.options.payoff(body)


@router.post(
    "/backtests",
    **JOB_CREATED,
    operation_id="startOptionsBacktest",
    dependencies=needs(Permission.LAB_RUN),
)
def start_backtest(
    body: OptionsBacktestRequest,
    services: ServicesDep,
    principal: PrincipalDep,
    response: Response,
) -> Job:
    """Queue an options backtest on the stored chains, with its validation
    checks unless ``validation`` is false. Fetch it from ``GET
    /api/options/backtests/{job_id}/result``."""
    return accepted(services.options.submit_backtest(body, owner_id=principal.user_id), response)


@router.get(
    "/backtests/{job_id}/result",
    response_model=OptionsBacktestView,
    operation_id="getOptionsBacktestResult",
    dependencies=needs(Permission.READ),
)
def get_backtest_result(
    job_id: str, services: ServicesDep, principal: OptionalPrincipalDep
) -> OptionsBacktestView:
    """The result of a succeeded options backtest job (409 until it has
    succeeded)."""
    return services.jobs.typed_result(job_id, OPTIONS_BACKTEST_JOB, OptionsBacktestView, principal)
