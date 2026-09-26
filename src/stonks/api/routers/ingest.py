from __future__ import annotations

from fastapi import APIRouter, Response

from stonks.api.deps import PageDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.api.routers._jobs_common import JOB_CREATED, accepted
from stonks.app.ingest import INGEST_JOB, IngestRequest, IngestResultView, IngestRunView
from stonks.app.jobs import Job
from stonks.app.pagination import Page
from stonks.auth import Permission

router = APIRouter(prefix="/api/ingest", tags=["ingest"], responses=PROBLEM_RESPONSES)


@router.get("/runs", response_model=Page[IngestRunView], operation_id="listIngestRuns")
def list_runs(
    services: ServicesDep,
    page: PageDep,
    kind: str | None = None,
    status: str | None = None,
) -> Page[IngestRunView]:
    return services.ingest.runs(kind=kind, status=status, limit=page.limit, offset=page.offset)


@router.post(
    "/runs",
    **JOB_CREATED,
    operation_id="startIngest",
    dependencies=needs(Permission.OPERATIONS_RUN),
)
def start_ingest(body: IngestRequest, services: ServicesDep, response: Response) -> Job:
    """Queue an ingest run; poll ``/api/jobs/{id}`` or stream its events."""
    return accepted(services.ingest.submit(body), response)


@router.get(
    "/jobs/{job_id}/result", response_model=IngestResultView, operation_id="getIngestResult"
)
def get_ingest_result(job_id: str, services: ServicesDep) -> IngestResultView:
    """The result of a succeeded ingest job (409 until it has succeeded)."""
    return services.jobs.typed_result(job_id, INGEST_JOB, IngestResultView)
