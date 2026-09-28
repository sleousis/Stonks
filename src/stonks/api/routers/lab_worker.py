"""``/api/lab/worker``: the lab queue for a lab worker on another machine
(roadmap 14.9, ``python -m stonks.lab.offload worker --api URL``).

Every route needs :attr:`Permission.LAB_WORKER`, which only a ``lab_worker``
token (minted by an admin) or an admin holds. A token with only that scope
reaches these routes and nothing else. See docs/deploy.md, Lab offload.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path
from fastapi.responses import StreamingResponse

from stonks.api.deps import PrincipalDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.jobs import Job
from stonks.app.lab_workers import (
    ClaimResult,
    HeartbeatReply,
    WorkerConfig,
    WorkerHeartbeat,
    WorkerHello,
    WorkerOutcome,
    WorkerRef,
    WorkerStopped,
)
from stonks.auth import Permission

router = APIRouter(prefix="/api/lab/worker", tags=["lab-worker"], responses=PROBLEM_RESPONSES)

_WORKER = needs(Permission.LAB_WORKER)
JobId = Annotated[str, Path(max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]

_TAR: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "The snapshot folder as an uncompressed tar",
        "content": {"application/x-tar": {"schema": {"type": "string", "format": "binary"}}},
    }
}


@router.post("/register", operation_id="registerLabWorker", dependencies=_WORKER)
def register_worker(
    body: WorkerHello, services: ServicesDep, principal: PrincipalDep
) -> WorkerConfig:
    """Add the worker (or restart it under the same id) and return the
    server's lease and heartbeat timing."""
    return services.lab_workers.register(principal, body)


@router.post("/claim", operation_id="claimLabWorkerJob", dependencies=_WORKER)
def claim_job(body: WorkerRef, services: ServicesDep, principal: PrincipalDep) -> ClaimResult:
    """Claim the oldest queued lab job a remote worker can run, with the
    snapshot to read and the research seed; ``job`` is null when none waits."""
    return services.lab_workers.claim(principal, body.worker_id)


@router.post("/jobs/{job_id}/heartbeat", operation_id="heartbeatLabWorkerJob", dependencies=_WORKER)
def heartbeat_job(
    job_id: JobId, body: WorkerHeartbeat, services: ServicesDep, principal: PrincipalDep
) -> HeartbeatReply:
    """Keep the job's lease and store its progress. The reply says whether
    a user asked to cancel it, or it is no longer this worker's."""
    return services.lab_workers.heartbeat(principal, job_id, body)


@router.post("/jobs/{job_id}/complete", operation_id="completeLabWorkerJob", dependencies=_WORKER)
def complete_job(
    job_id: JobId, body: WorkerOutcome, services: ServicesDep, principal: PrincipalDep
) -> Job:
    """Store the job's outcome and import the ledger rows and strategies it
    wrote (409 when the job is no longer this worker's)."""
    return services.lab_workers.complete(principal, job_id, body)


@router.post("/jobs/{job_id}/release", operation_id="releaseLabWorkerJob", dependencies=_WORKER)
def release_job(
    job_id: JobId, body: WorkerRef, services: ServicesDep, principal: PrincipalDep
) -> Job:
    """A stopping worker hands its running job back to the queue (it ends
    cancelled instead when a user asked to cancel it)."""
    return services.lab_workers.release(principal, job_id, body.worker_id)


@router.post("/stop", operation_id="stopLabWorker", dependencies=_WORKER)
def stop_worker(body: WorkerRef, services: ServicesDep, principal: PrincipalDep) -> WorkerStopped:
    """Mark the worker stopped (health and metrics stop counting it)."""
    return services.lab_workers.stop(principal, body.worker_id)


@router.get(
    "/snapshots/{name}",
    operation_id="downloadLabSnapshot",
    dependencies=_WORKER,
    response_class=StreamingResponse,
    responses=_TAR,
)
def download_snapshot(
    name: Annotated[str, Path(max_length=64, pattern=r"^[0-9A-Za-z][0-9A-Za-z_-]*$")],
    services: ServicesDep,
    principal: PrincipalDep,
) -> StreamingResponse:
    """A read-only lake snapshot, streamed as a tar. The worker keeps it
    until a newer one is named in a claim."""
    chunks = services.lab_workers.snapshot_archive(principal, name)
    return StreamingResponse(
        chunks,
        media_type="application/x-tar",
        headers={"Content-Disposition": f'attachment; filename="{name}.tar"'},
    )
