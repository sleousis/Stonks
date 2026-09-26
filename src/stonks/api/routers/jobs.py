from __future__ import annotations

import time
from collections.abc import AsyncIterable
from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlencode

import anyio
from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel

from stonks.api.deps import PageDep, PrincipalDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.jobs import Job, JobStatus
from stonks.app.pagination import Page
from stonks.auth import Permission

router = APIRouter(prefix="/api/jobs", tags=["jobs"], responses=PROBLEM_RESPONSES)
#: The event stream authorizes itself (bearer *or* a job-scoped stream token).
events_router = APIRouter(prefix="/api/jobs", tags=["jobs"], responses=PROBLEM_RESPONSES)


class JobEvent(BaseModel):
    """One ``data:`` payload of the job event stream."""

    job_id: str
    status: JobStatus
    progress: float
    message: str | None = None
    error: str | None = None
    #: Set on the final ``end`` event when the stream stops before the job
    #: finished: ``timeout`` (``api.sse_max_stream_seconds`` elapsed) or
    #: ``untracked`` (no worker will update this job any more; poll
    #: ``GET /api/jobs/{id}`` or restart the server to recover it).
    reason: Literal["timeout", "untracked"] | None = None


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


@router.post(
    "/{job_id}/cancel",
    response_model=Job,
    operation_id="cancelJob",
    dependencies=needs(Permission.LAB_RUN),
)
def cancel_job(job_id: str, services: ServicesDep, principal: PrincipalDep) -> Job:
    """Cancel a queued job, or ask a running lab run to stop at its next
    trial (it ends ``cancelled``). Other running jobs cannot be interrupted
    (409). Ticks, ingests and backups need an admin."""
    return services.jobs.cancel(job_id, principal)


class StreamToken(BaseModel):
    token: str
    expires_at: datetime
    #: The events URL with the token attached, ready for ``new EventSource``.
    events_url: str


@router.post(
    "/{job_id}/stream-token",
    response_model=StreamToken,
    operation_id="createStreamToken",
    dependencies=needs(Permission.READ),
)
def create_stream_token(job_id: str, services: ServicesDep, principal: PrincipalDep) -> StreamToken:
    """A short-lived token (``api.stream_token_ttl_seconds``) that lets a
    client which cannot send the bearer header (browser ``EventSource``)
    read this job's event stream, and nothing else. It is tied to your
    user and stops working if your account is disabled."""
    issued = services.jobs.stream_token(job_id, principal)
    return StreamToken(
        token=issued.token,
        expires_at=issued.expires_at,
        events_url=f"/api/jobs/{job_id}/events?{urlencode({'token': issued.token})}",
    )


def _existing_job(job_id: str, services: ServicesDep) -> Job:
    # Resolved before the stream starts so an unknown id is a normal 404
    # instead of an error in the middle of a 200 event stream.
    return services.jobs.get(job_id)


@events_router.get(
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

    Events: ``status`` on every change, then ``done`` when the job reaches
    a terminal status, or ``end`` (with ``reason``) when the stream gives up
    first: after ``api.sse_max_stream_seconds``, or when no worker in this
    process tracks the job any more. The stream also ends when the client
    disconnects; the store is polled off the event loop.
    """
    poll = float(request.app.state.sse_poll_seconds)
    max_seconds = services.context.settings.api.sse_max_stream_seconds
    deadline = time.monotonic() + max_seconds
    last: tuple | None = None
    seq = 0
    untracked_polls = 0
    current = job
    while True:
        snapshot = (current.status, current.progress, current.message)
        if snapshot != last:
            last = snapshot
            seq += 1
            kind: Literal["status", "done"] = "done" if current.is_terminal else "status"
            yield ServerSentEvent(data=_event(current), event=kind, id=str(seq))
        if current.is_terminal or await request.is_disconnected():
            return
        # Two consecutive polls, so a job between row insert and hand-off
        # to a worker is not mistaken for an orphan.
        untracked_polls = 0 if services.jobs.is_tracked(current.id) else untracked_polls + 1
        reason: Literal["timeout", "untracked"] | None = None
        if untracked_polls >= 2:
            reason = "untracked"
        elif time.monotonic() >= deadline:
            reason = "timeout"
        if reason is not None:
            yield ServerSentEvent(data=_event(current, reason), event="end", id=str(seq + 1))
            return
        await anyio.sleep(poll)
        current = await run_in_threadpool(services.jobs.get, current.id)


def _event(job: Job, reason: Literal["timeout", "untracked"] | None = None) -> JobEvent:
    return JobEvent(
        job_id=job.id,
        status=job.status,
        progress=job.progress,
        message=job.message,
        error=job.error,
        reason=reason,
    )
