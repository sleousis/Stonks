from __future__ import annotations

from collections.abc import AsyncIterable
from typing import Annotated, Literal

import anyio
from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel

from stonks.api.deps import PageDep, ServicesDep
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.jobs import Job, JobStatus
from stonks.app.pagination import Page

router = APIRouter(prefix="/api/jobs", tags=["jobs"], responses=PROBLEM_RESPONSES)


class JobEvent(BaseModel):
    """One ``data:`` payload of the job event stream."""

    job_id: str
    status: JobStatus
    progress: float
    message: str | None = None
    error: str | None = None


@router.get("", response_model=Page[Job], operation_id="listJobs")
def list_jobs(
    services: ServicesDep,
    page: PageDep,
    status: JobStatus | None = None,
    kind: str | None = None,
) -> Page[Job]:
    return services.jobs.list(status=status, kind=kind, limit=page.limit, offset=page.offset)


@router.get("/{job_id}", response_model=Job, operation_id="getJob")
def get_job(job_id: str, services: ServicesDep) -> Job:
    return services.jobs.get(job_id)


@router.post("/{job_id}/cancel", response_model=Job, operation_id="cancelJob")
def cancel_job(job_id: str, services: ServicesDep) -> Job:
    """Cancel a queued job, or ask a running lab run to stop at its next
    trial (it ends ``cancelled``). Other running jobs cannot be interrupted
    (409)."""
    return services.jobs.cancel(job_id)


def _existing_job(job_id: str, services: ServicesDep) -> Job:
    # Resolved before the stream starts so an unknown id is a normal 404
    # instead of an error in the middle of a 200 event stream.
    return services.jobs.get(job_id)


@router.get(
    "/{job_id}/events",
    response_class=EventSourceResponse,
    operation_id="streamJobEvents",
)
async def stream_job_events(
    request: Request,
    services: ServicesDep,
    job: Annotated[Job, Depends(_existing_job)],
) -> AsyncIterable[ServerSentEvent]:
    """Server-sent events with the job's status/progress until it finishes.

    The stream ends when the job reaches a terminal status or the client
    disconnects; the store is polled off the event loop.
    """
    poll = float(request.app.state.sse_poll_seconds)
    last: tuple | None = None
    seq = 0
    current = job
    while True:
        snapshot = (current.status, current.progress, current.message)
        if snapshot != last:
            last = snapshot
            seq += 1
            kind: Literal["status", "done"] = "done" if current.is_terminal else "status"
            yield ServerSentEvent(
                data=JobEvent(
                    job_id=current.id,
                    status=current.status,
                    progress=current.progress,
                    message=current.message,
                    error=current.error,
                ),
                event=kind,
                id=str(seq),
            )
        if current.is_terminal or await request.is_disconnected():
            return
        await anyio.sleep(poll)
        current = await run_in_threadpool(services.jobs.get, current.id)
