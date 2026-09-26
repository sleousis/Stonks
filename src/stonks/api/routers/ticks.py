from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Response

from stonks.api.deps import PageDep, PrincipalDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.api.routers._jobs_common import JOB_CREATED, accepted
from stonks.app.jobs import Job
from stonks.app.pagination import Page
from stonks.app.tick_summary import TickRun, TickRunWithOrders, typed_tick_run
from stonks.app.ticks import TICK_JOB, TickRequest, TickResultView, TickStatus
from stonks.auth import Permission

router = APIRouter(prefix="/api/ticks", tags=["ticks"], responses=PROBLEM_RESPONSES)


@router.get("", response_model=Page[TickRun], operation_id="listTicks")
def list_ticks(
    services: ServicesDep,
    principal: PrincipalDep,
    page: PageDep,
    status: TickStatus | None = None,
) -> Page[TickRun]:
    """Every tick run, newest first. Summaries keep the global outcome
    and the parts about your own portfolios only."""
    runs = services.ticks.list(principal, status=status, limit=page.limit, offset=page.offset)
    return Page[TickRun](
        items=[typed_tick_run(r) for r in runs.items],
        total=runs.total,
        limit=runs.limit,
        offset=runs.offset,
    )


@router.get("/jobs/{job_id}/result", response_model=TickResultView, operation_id="getTickResult")
def get_tick_result(job_id: str, services: ServicesDep) -> TickResultView:
    """The result of a succeeded tick job (409 until it has succeeded)."""
    return services.jobs.typed_result(job_id, TICK_JOB, TickResultView)


@router.get("/{tick_id}", response_model=TickRunWithOrders, operation_id="getTick")
def get_tick(
    tick_id: str,
    services: ServicesDep,
    principal: PrincipalDep,
    portfolio_id: Annotated[
        str | None,
        Query(max_length=64, description="One of your portfolios (404 otherwise)."),
    ] = None,
) -> TickRun:
    """One tick run with the orders it placed in your portfolios (or in
    ``portfolio_id`` only). Another user's portfolio is a 404."""
    return typed_tick_run(services.ticks.get(principal, tick_id, portfolio_id=portfolio_id))


@router.post(
    "", **JOB_CREATED, operation_id="startTick", dependencies=needs(Permission.OPERATIONS_RUN)
)
def start_tick(body: TickRequest, services: ServicesDep, response: Response) -> Job:
    """Queue a production tick (``dry_run`` ranks and logs only)."""
    return accepted(services.ticks.submit(body), response)
