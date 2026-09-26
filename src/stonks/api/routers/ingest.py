from __future__ import annotations

from fastapi import APIRouter, Response

from stonks.api.deps import PageDep, ServicesDep
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.api.routers._jobs_common import JOB_CREATED, accepted
from stonks.app.ingest import IngestRequest, IngestRunView
from stonks.app.jobs import Job
from stonks.app.pagination import Page

router = APIRouter(prefix="/api/ingest", tags=["ingest"], responses=PROBLEM_RESPONSES)


@router.get("/runs", response_model=Page[IngestRunView], operation_id="listIngestRuns")
def list_runs(
    services: ServicesDep,
    page: PageDep,
    kind: str | None = None,
    status: str | None = None,
) -> Page[IngestRunView]:
    return services.ingest.runs(kind=kind, status=status, limit=page.limit, offset=page.offset)


@router.post("/runs", **JOB_CREATED, operation_id="startIngest")
def start_ingest(body: IngestRequest, services: ServicesDep, response: Response) -> Job:
    """Queue an ingest run; poll ``/api/jobs/{id}`` or stream its events."""
    return accepted(services.ingest.submit(body), response)
