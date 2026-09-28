"""The kill switch and risk halts (roadmap 12.6, BL-28; design section 9).

Every route takes the caller (a principal), so each needs a credential,
reads included. Another user's portfolio or halt is a 404. The service
writes the audit rows."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from stonks.api.deps import PageDep, PrincipalDep, ServicesDep, client_ip, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.halts import (
    ClearHaltRequest,
    HaltService,
    HaltView,
    KillSwitchRequest,
    ResumeChecksView,
    ResumeRequest,
)
from stonks.app.pagination import Page, page_of
from stonks.auth import Permission

router = APIRouter(prefix="/api/halts", tags=["halts"], responses=PROBLEM_RESPONSES)


def get_halts(services: ServicesDep) -> HaltService:
    return HaltService(services.context)


HaltsDep = Annotated[HaltService, Depends(get_halts)]


@router.get("", response_model=Page[HaltView], operation_id="listHalts")
def list_halts(
    halts: HaltsDep,
    principal: PrincipalDep,
    page: PageDep,
    include_cleared: Annotated[bool, Query(description="also cleared and expired halts")] = False,
) -> Page[HaltView]:
    """Halts you can see, newest first: global ones, your own and those of
    your portfolios. By default only those in force today."""
    return page_of(halts.list(principal, include_cleared=include_cleared), page)


@router.get("/{halt_id}", response_model=HaltView, operation_id="getHalt")
def get_halt(halt_id: int, halts: HaltsDep, principal: PrincipalDep) -> HaltView:
    return halts.get(principal, halt_id)


@router.get(
    "/{halt_id}/resume-checks",
    response_model=ResumeChecksView,
    operation_id="getResumeChecks",
    dependencies=needs(Permission.KILLSWITCH_USER),
)
def get_resume_checks(halt_id: int, halts: HaltsDep, principal: PrincipalDep) -> ResumeChecksView:
    """What a resume of this kill switch checks, read now: per covered
    portfolio at a real broker, the gateway is up, the last reconcile was
    clean, the account reads and its equity covers a multiple of the
    largest position. Read only."""
    return halts.resume_checks(principal, halt_id)


@router.post(
    "/kill",
    status_code=201,
    response_model=HaltView,
    operation_id="engageKillSwitch",
    dependencies=needs(Permission.KILLSWITCH_USER),
)
def engage_kill_switch(
    body: KillSwitchRequest, request: Request, halts: HaltsDep, principal: PrincipalDep
) -> HaltView:
    """Stop new orders: every portfolio (``global``, admins only), all of
    yours (``user``) or one of yours (``portfolio``). ``buys_only`` stops
    buys only, so sells and exits still go through and no position is
    closed (``flatten`` is its deprecated name). Returns the open kill switch
    when one is already on at that scope."""
    return halts.engage_kill(principal, body, ip=client_ip(request))


@router.post(
    "/{halt_id}/resume",
    response_model=HaltView,
    operation_id="resumeKillSwitch",
    dependencies=needs(Permission.KILLSWITCH_RESUME),
)
def resume_kill_switch(
    halt_id: int, body: ResumeRequest, request: Request, halts: HaltsDep, principal: PrincipalDep
) -> HaltView:
    """Turn a kill switch off. ``confirmation`` must be exactly
    ``RESUME TRADING``; the reason is audited. Needs a second factor from
    the last few minutes (step-up), so API tokens get 403. A failed resume
    check is a 409 unless ``override_checks`` is set (audited)."""
    return halts.resume_kill(principal, halt_id, body, ip=client_ip(request))


@router.post(
    "/{halt_id}/clear",
    response_model=HaltView,
    operation_id="clearHalt",
    dependencies=needs(Permission.RISK_RESET),
)
def clear_halt(
    halt_id: int, body: ClearHaltRequest, request: Request, halts: HaltsDep, principal: PrincipalDep
) -> HaltView:
    """The logged reset of a circuit-breaker or operational halt (not the
    kill switch). A latched drawdown halt stays until this is called."""
    return halts.clear(principal, halt_id, body, ip=client_ip(request))
