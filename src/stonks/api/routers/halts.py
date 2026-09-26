"""The kill switch and risk halts (roadmap 12.6, BL-28; design section 9).

Every route takes the caller's scope, so each needs the bearer token,
reads included. Another user's portfolio or halt is a 404. The service
writes the audit rows."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from stonks.api.deps import ScopeDep, ServicesDep
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.halts import (
    ClearHaltRequest,
    HaltService,
    HaltView,
    KillSwitchRequest,
    ResumeRequest,
)

router = APIRouter(prefix="/api/halts", tags=["halts"], responses=PROBLEM_RESPONSES)


def get_halts(services: ServicesDep) -> HaltService:
    return HaltService(services.context)


HaltsDep = Annotated[HaltService, Depends(get_halts)]


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.get("", response_model=list[HaltView], operation_id="listHalts")
def list_halts(
    halts: HaltsDep,
    scope: ScopeDep,
    include_cleared: Annotated[bool, Query(description="also cleared and expired halts")] = False,
) -> list[HaltView]:
    """Halts you can see, newest first: global ones, your own and those of
    your portfolios. By default only those in force today."""
    return halts.list(scope, include_cleared=include_cleared)


@router.get("/{halt_id}", response_model=HaltView, operation_id="getHalt")
def get_halt(halt_id: int, halts: HaltsDep, scope: ScopeDep) -> HaltView:
    return halts.get(scope, halt_id)


@router.post("/kill", status_code=201, response_model=HaltView, operation_id="engageKillSwitch")
def engage_kill_switch(
    body: KillSwitchRequest, request: Request, halts: HaltsDep, scope: ScopeDep
) -> HaltView:
    """Stop new orders: every portfolio (``global``, admins only), all of
    yours (``user``) or one of yours (``portfolio``). ``flatten`` stops buys
    only, so sells and exits still go through. Returns the open kill switch
    when one is already on at that scope."""
    return halts.engage_kill(scope, body, ip=_ip(request))


@router.post("/{halt_id}/resume", response_model=HaltView, operation_id="resumeKillSwitch")
def resume_kill_switch(
    halt_id: int, body: ResumeRequest, request: Request, halts: HaltsDep, scope: ScopeDep
) -> HaltView:
    """Turn a kill switch off. ``confirmation`` must be exactly
    ``RESUME TRADING``; the reason is audited."""
    return halts.resume_kill(scope, halt_id, body, ip=_ip(request))


@router.post("/{halt_id}/clear", response_model=HaltView, operation_id="clearHalt")
def clear_halt(
    halt_id: int, body: ClearHaltRequest, request: Request, halts: HaltsDep, scope: ScopeDep
) -> HaltView:
    """The logged reset of a circuit-breaker or operational halt (not the
    kill switch). A latched drawdown halt stays until this is called."""
    return halts.clear(scope, halt_id, body, ip=_ip(request))
