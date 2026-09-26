from __future__ import annotations

from fastapi import APIRouter, Response

from stonks.api.deps import PageDep, ServicesDep
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.api.routers._jobs_common import JOB_CREATED, accepted
from stonks.app.jobs import Job
from stonks.app.pagination import Page
from stonks.app.ticks import TickRequest, TickRunDetail, TickRunView

router = APIRouter(prefix="/api/ticks", tags=["ticks"], responses=PROBLEM_RESPONSES)


@router.get("", response_model=Page[TickRunView], operation_id="listTicks")
def list_ticks(
    services: ServicesDep, page: PageDep, status: str | None = None
) -> Page[TickRunView]:
    return services.ticks.list(status=status, limit=page.limit, offset=page.offset)


@router.get("/{tick_id}", response_model=TickRunDetail, operation_id="getTick")
def get_tick(tick_id: str, services: ServicesDep) -> TickRunDetail:
    return services.ticks.get(tick_id)


@router.post("", **JOB_CREATED, operation_id="startTick")
def start_tick(body: TickRequest, services: ServicesDep, response: Response) -> Job:
    """Queue a production tick (``dry_run`` ranks and logs only)."""
    return accepted(services.ticks.submit(body), response)
