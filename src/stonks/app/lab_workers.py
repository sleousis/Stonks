"""The lab queue over the API, for a lab worker on another machine (14.9).

A worker that shares the server's data folder pulls jobs straight from the
state DB (:mod:`stonks.lab.offload.worker`). A worker on another machine
(``python -m stonks.lab.offload worker --api URL``) cannot: SQLite over a
network is not safe, and the lake is locked by the API. It calls these
methods through ``/api/lab/worker/*`` with a ``lab_worker`` token instead,
and they reuse the same queue logic (:class:`~stonks.lab.offload.queue.LabQueue`):

1. ``register``: add or restart the worker row, and tell it the lease;
2. ``claim``: reap lost jobs, claim the oldest portable job, and hand it
   out with the current snapshot name and the research seed;
3. ``snapshot_archive``: stream that snapshot (the worker caches it);
4. ``heartbeat``: keep the lease, store progress, report a cancel;
5. ``complete``: import the research rows the job wrote, store its result;
6. ``release``: a stopping worker hands its job back to the queue;
7. ``stop``: mark the worker stopped.

Every method needs :attr:`Permission.LAB_WORKER`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from stonks.app.context import AppContext
from stonks.app.errors import ConflictError, NotFoundError
from stonks.app.jobs import Job, JobRunner
from stonks.auth import Permission, Principal, require
from stonks.ingest.redact import redact_secrets
from stonks.lab.offload.queue import LabQueue
from stonks.lab.offload.research_sync import ResearchRows, apply_rows, build_seed
from stonks.lab.offload.snapshot import LakeSnapshots
from stonks.lab.offload.transfer import REMOTE_KINDS, iter_snapshot_tar
from stonks.logging import get_logger

_log = get_logger("stonks.app.lab_workers")

WORKER_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"


class WorkerHello(BaseModel):
    worker_id: str = Field(pattern=WORKER_ID_PATTERN)
    host: str = Field(min_length=1, max_length=255)
    pid: int = Field(ge=0)
    cpus: int = Field(ge=1, le=4096)


class WorkerRef(BaseModel):
    worker_id: str = Field(pattern=WORKER_ID_PATTERN)


class WorkerConfig(BaseModel):
    """The server's ``[lab.offload]`` timing, which the worker follows."""

    executor: str
    #: Kinds a remote worker takes (``[lab.offload] kinds`` that need no
    #: server-side files: lab runs and sweeps).
    kinds: list[str]
    heartbeat_seconds: float
    poll_seconds: float
    lease_seconds: float


class WorkerStopped(BaseModel):
    worker_id: str
    stopped: bool


class SnapshotRef(BaseModel):
    """A lake snapshot by folder name; fetch it from
    ``GET /api/lab/worker/snapshots/{name}``."""

    name: str
    created_at: datetime
    fingerprint: str


class ClaimedJob(BaseModel):
    id: str
    kind: str
    params: dict[str, Any]
    owner_id: str | None
    created_at: datetime
    snapshot: SnapshotRef
    #: The trial ledger and the active strategies with their artifacts.
    seed: ResearchRows


class ClaimResult(BaseModel):
    #: ``null`` when nothing waits.
    job: ClaimedJob | None = None


class WorkerHeartbeat(BaseModel):
    worker_id: str = Field(pattern=WORKER_ID_PATTERN)
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    message: str | None = Field(default=None, max_length=500)


class HeartbeatReply(BaseModel):
    #: False when the job is no longer this worker's running job (it was
    #: reaped, finished or released): stop it and do not report it.
    running: bool
    cancel_requested: bool


class WorkerOutcome(BaseModel):
    worker_id: str = Field(pattern=WORKER_ID_PATTERN)
    status: Literal["succeeded", "failed", "cancelled"]
    #: The job's result (``succeeded``), as the handler returned it.
    result: Any = None
    error: str | None = Field(default=None, max_length=20_000)
    #: Ledger rows, trial matrices and registered strategies the job wrote.
    research: ResearchRows = Field(default_factory=ResearchRows)


class LabWorkerService:
    def __init__(self, context: AppContext, runner: JobRunner) -> None:
        self._ctx = context
        self._runner = runner

    @property
    def _offload(self) -> Any:
        return self._ctx.settings.lab.offload

    def _queue(self) -> LabQueue:
        return LabQueue(self._ctx.settings.state.path)

    def _snapshots(self) -> LakeSnapshots:
        offload = self._offload
        return LakeSnapshots(
            offload.snapshot_root(self._ctx.settings.lake.path), keep=offload.keep_snapshots
        )

    def config(self) -> WorkerConfig:
        offload = self._offload
        return WorkerConfig(
            executor=offload.executor,
            kinds=[k for k in offload.kinds if k in REMOTE_KINDS],
            heartbeat_seconds=offload.heartbeat_seconds,
            poll_seconds=offload.poll_seconds,
            lease_seconds=offload.lease_seconds,
        )

    # ---- worker lifecycle ------------------------------------------------------

    def register(self, principal: Principal, hello: WorkerHello) -> WorkerConfig:
        require(principal, Permission.LAB_WORKER)
        self._queue().register_worker(
            hello.worker_id, host=f"remote:{hello.host}", pid=hello.pid, cpus=hello.cpus
        )
        _log.info("lab_worker.remote_registered", worker_id=hello.worker_id, actor=principal.actor)
        return self.config()

    def stop(self, principal: Principal, worker_id: str) -> WorkerStopped:
        require(principal, Permission.LAB_WORKER)
        self._queue().stop_worker(worker_id)
        _log.info("lab_worker.remote_stopped", worker_id=worker_id)
        return WorkerStopped(worker_id=worker_id, stopped=True)

    # ---- jobs --------------------------------------------------------------------

    def claim(self, principal: Principal, worker_id: str) -> ClaimResult:
        require(principal, Permission.LAB_WORKER)
        queue = self._queue()
        offload = self._offload
        reaped = queue.reap_stale(offload.lease_seconds)
        if reaped:
            _log.warning("lab_worker.reaped_lost_jobs", jobs=reaped)
        queue.beat_worker(worker_id, None)
        kinds = self.config().kinds
        job_id = queue.claim_next(worker_id, kinds, portable_only=True)
        if job_id is None:
            return ClaimResult()
        store = self._runner.store
        info = self._snapshots().current()
        if info is None:
            store.finish(job_id, "failed", error="no lake snapshot for the lab worker to read")
            queue.worker_done(worker_id, "failed")
            _log.error("lab_worker.no_snapshot", job_id=job_id, worker_id=worker_id)
            return ClaimResult()
        try:
            with self._ctx.state() as state:
                seed = build_seed(state, self._ctx.settings.registry.artifacts_dir)
            job = store.get(job_id)
        except Exception:
            queue.release(job_id, worker_id)  # let a later claim try again
            raise
        queue.beat_worker(worker_id, job_id)
        _log.info("lab_worker.remote_claimed", job_id=job_id, worker_id=worker_id)
        return ClaimResult(
            job=ClaimedJob(
                id=job.id,
                kind=job.kind,
                params=job.params,
                owner_id=job.owner_id,
                created_at=job.created_at,
                snapshot=SnapshotRef(
                    name=info.directory.name,
                    created_at=info.created_at,
                    fingerprint=info.fingerprint,
                ),
                seed=seed,
            )
        )

    def heartbeat(self, principal: Principal, job_id: str, beat: WorkerHeartbeat) -> HeartbeatReply:
        require(principal, Permission.LAB_WORKER)
        queue = self._queue()
        running = queue.is_running_on(job_id, beat.worker_id)
        cancel = queue.cancel_requested(job_id)
        if running:
            queue.heartbeat(job_id, beat.worker_id)
            # Written only while no cancel is flagged, so the
            # "cancellation requested" message is never overwritten.
            queue.worker_progress(job_id, beat.worker_id, beat.progress, beat.message)
        queue.beat_worker(beat.worker_id, job_id if running else None)
        return HeartbeatReply(running=running, cancel_requested=cancel)

    def complete(self, principal: Principal, job_id: str, outcome: WorkerOutcome) -> Job:
        """Import what the job wrote, then store its final status. A job that
        is no longer this worker's running job is a :class:`ConflictError`,
        and nothing is written."""
        require(principal, Permission.LAB_WORKER)
        queue = self._queue()
        if not queue.is_running_on(job_id, outcome.worker_id):
            raise ConflictError(f"job {job_id} is not running on worker {outcome.worker_id}")
        with self._ctx.state() as state:
            applied = apply_rows(state, self._ctx.settings.registry.artifacts_dir, outcome.research)
        error = outcome.error
        if error is not None:
            error = redact_secrets(error, self._runner.secrets())
        status = outcome.status
        result = outcome.result if status == "succeeded" else None
        self._runner.store.finish(job_id, status, result=result, error=error)
        queue.worker_done(outcome.worker_id, status)
        _log.info(
            "lab_worker.remote_finished",
            job_id=job_id,
            worker_id=outcome.worker_id,
            outcome=status,
            lab_runs=applied.lab_runs,
            strategies=applied.strategies,
        )
        return self._runner.store.get(job_id)

    def release(self, principal: Principal, job_id: str, worker_id: str) -> Job:
        """Put a stopping worker's running job back in the queue. A job a
        user asked to cancel ends ``cancelled`` instead."""
        require(principal, Permission.LAB_WORKER)
        queue = self._queue()
        if queue.release(job_id, worker_id):
            _log.info("lab_worker.job_requeued", job_id=job_id, reason="worker stopping")
        elif queue.is_running_on(job_id, worker_id):  # a cancel was requested
            self._runner.store.finish(job_id, "cancelled", error="cancelled while running")
            queue.worker_done(worker_id, "cancelled")
        else:
            raise ConflictError(f"job {job_id} is not running on worker {worker_id}")
        return self._runner.store.get(job_id)

    # ---- data --------------------------------------------------------------------

    def snapshot_archive(self, principal: Principal, name: str) -> Iterator[bytes]:
        """Snapshot ``name`` as a tar stream, held against pruning while it
        is sent."""
        require(principal, Permission.LAB_WORKER)
        snapshots = self._snapshots()
        info = snapshots.get(name)
        if info is None:
            raise NotFoundError(f"no lake snapshot {name!r}")

        def stream() -> Iterator[bytes]:
            with snapshots.hold(info, f"download-{uuid.uuid4().hex[:12]}"):
                yield from iter_snapshot_tar(snapshots, info)

        return stream()
