from __future__ import annotations

from fastapi import APIRouter, Response

from stonks.api.deps import PageDep, ServicesDep
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.api.routers._jobs_common import JOB_CREATED, accepted
from stonks.app.jobs import Job
from stonks.app.pagination import Page
from stonks.app.tick_summary import TickRun, TickRunWithOrders, typed_tick_run
from stonks.app.ticks import TICK_JOB, TickRequest, TickResultView

router = APIRouter(prefix="/api/ticks", tags=["ticks"], responses=PROBLEM_RESPONSES)


@router.get("", response_model=Page[TickRun], operation_id="listTicks")
def list_ticks(services: ServicesDep, page: PageDep, status: str | None = None) -> Page[TickRun]:
    runs = services.ticks.list(status=status, limit=page.limit, offset=page.offset)
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
def get_tick(tick_id: str, services: ServicesDep) -> TickRun:
    return typed_tick_run(services.ticks.get(tick_id))


@router.post("", **JOB_CREATED, operation_id="startTick")
def start_tick(body: TickRequest, services: ServicesDep, response: Response) -> Job:
    """Queue a production tick (``dry_run`` ranks and logs only)."""
    return accepted(services.ticks.submit(body), response)
