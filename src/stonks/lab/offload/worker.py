"""The lab worker (roadmap 14.9): ``python -m stonks.lab.offload worker``.

A separate process (usually the ``lab-worker`` Compose service) that pulls
worker jobs from the state DB queue and runs them with the same handlers
the API would use, against the current read-only lake snapshot:

1. fail running jobs whose worker stopped sending heartbeats;
2. claim the oldest queued job of a known kind (atomic across workers);
3. pin the current snapshot for the whole job and mark it held;
4. run the handler through ``JobRunner.run_claimed``, so errors, secret
   scrubbing, cancellation and the result row work as for any job;
5. meanwhile a heartbeat thread keeps the lease, refreshes the hold and
   turns a cancel request from the API into a cooperative cancel.

One job runs at a time per worker; the job itself fans out over the
worker's cores through the lab's process pool (``[lab.parallel]``). Run
more workers to run more jobs at once.
"""

from __future__ import annotations

import os
import socket
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from stonks.app.context import AppContext
from stonks.app.errors import ConflictError
from stonks.app.jobs import JobContext, JobRunner
from stonks.config import Settings
from stonks.lab.offload.queue import LabQueue
from stonks.lab.offload.settings import LabOffloadSettings
from stonks.lab.offload.snapshot import LakeSnapshots, SnapshotInfo
from stonks.logging import get_logger
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState

_log = get_logger("stonks.lab.offload.worker")


class SnapshotContext(AppContext):
    """An :class:`AppContext` whose lake is a read-only snapshot. The state
    DB and artifacts are the real ones: results, trial ledgers and
    registered strategies land where the API reads them."""

    def __init__(self, settings: Settings, snapshots: LakeSnapshots) -> None:
        super().__init__(settings)
        self.snapshots = snapshots
        self._pinned: SnapshotInfo | None = None

    def start(self) -> None:
        with SqliteState(self.settings.state.path) as state:
            state.migrate()

    def close(self) -> None:
        return None

    @contextmanager
    def pinned(self, info: SnapshotInfo) -> Iterator[None]:
        """Every lake opened inside is ``info``, even if a newer one appears."""
        self._pinned = info
        try:
            yield
        finally:
            self._pinned = None

    @contextmanager
    def lake(self) -> Iterator[DuckDBLake]:
        info = self._pinned or self.snapshots.current()
        if info is None:
            raise ConflictError("no lake snapshot yet: the API publishes one when it queues a job")
        lake = info.open()
        try:
            yield lake
        finally:
            lake.close()


class _Heartbeat:
    """Background beats for one running job."""

    def __init__(
        self,
        worker: LabWorker,
        job_id: str,
        ctx: JobContext,
        touch: Any,
    ) -> None:
        self._worker = worker
        self._job_id = job_id
        self._ctx = ctx
        self._touch = touch
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name=f"lab-heartbeat-{job_id}", daemon=True
        )

    def __enter__(self) -> _Heartbeat:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.wait(self._worker.settings.heartbeat_seconds):
            self.beat()

    def beat(self) -> None:
        worker = self._worker
        try:
            worker.queue.heartbeat(self._job_id, worker.worker_id)
            worker.queue.beat_worker(worker.worker_id, self._job_id)
            self._touch()
            if worker.queue.cancel_requested(self._job_id):
                self._ctx.request_cancel()
        except Exception as exc:  # a missed beat is retried on the next one
            _log.warning("lab_worker.heartbeat_failed", job_id=self._job_id, error=str(exc))


class LabWorker:
    def __init__(
        self,
        settings: LabOffloadSettings,
        *,
        runner: JobRunner,
        context: SnapshotContext,
        queue: LabQueue,
        worker_id: str | None = None,
        cpus: int | None = None,
    ) -> None:
        self.settings = settings
        self.runner = runner
        self.context = context
        self.queue = queue
        self.worker_id = worker_id or f"lw_{socket.gethostname()}_{uuid.uuid4().hex[:8]}"
        self.cpus = cpus or os.cpu_count() or 1
        self._current: JobContext | None = None
        self._registered = False
        self._stopping = False

    @property
    def snapshots(self) -> LakeSnapshots:
        return self.context.snapshots

    def register(self) -> None:
        if not self._registered:
            self.queue.register_worker(
                self.worker_id, host=socket.gethostname(), pid=os.getpid(), cpus=self.cpus
            )
            self._registered = True
            _log.info("lab_worker.started", worker_id=self.worker_id, kinds=self.settings.kinds)

    def run_once(self) -> str | None:
        """Reap, then claim and run one job; its id, or ``None`` when idle."""
        self.register()
        reaped = self.queue.reap_stale(self.settings.lease_seconds)
        if reaped:
            _log.warning("lab_worker.reaped_lost_jobs", jobs=reaped)
        self.queue.beat_worker(self.worker_id, None)
        job_id = self.queue.claim_next(self.worker_id, self.settings.kinds)
        if job_id is None:
            return None
        self._run(job_id)
        return job_id

    def _run(self, job_id: str) -> None:
        store = self.runner.store
        ctx = JobContext(job_id=job_id, _store=store)
        log = _log.bind(job_id=job_id, worker_id=self.worker_id)
        info = self.snapshots.current()
        if info is None:
            store.finish(job_id, "failed", error="no lake snapshot for the lab worker to read")
            self.queue.worker_done(self.worker_id, "failed")
            log.error("lab_worker.no_snapshot")
            return
        log.info("lab_worker.job_started", snapshot=info.directory.name)
        self._current = ctx
        try:
            with (
                self.snapshots.hold(info, self.worker_id) as touch,
                self.context.pinned(info),
                _Heartbeat(self, job_id, ctx, touch),
            ):
                outcome = self.runner.run_claimed(job_id, ctx)
        finally:
            self._current = None
        if (
            outcome == "cancelled"
            and self._stopping
            and not self.queue.cancel_requested(job_id)
            and self.queue.requeue(job_id, self.worker_id)
        ):
            # stopped by a shutdown, not a user: another worker (or this
            # one after its restart) runs it again (BE-42)
            log.info("lab_worker.job_requeued", reason="worker stopping")
            return
        self.queue.worker_done(self.worker_id, outcome)  # type: ignore[arg-type]
        log.info("lab_worker.job_finished", outcome=outcome)

    def request_stop(self) -> None:
        """Stop the running job (if any) at its next checkpoint and hand it
        back to the queue."""
        self._stopping = True
        if self._current is not None:
            self._current.request_cancel()

    def run_forever(self, stop: threading.Event) -> None:
        self.register()
        try:
            while not stop.is_set():
                try:
                    busy = self.run_once() is not None
                except Exception as exc:  # a locked DB: log and poll again
                    _log.error("lab_worker.loop_error", error=str(exc))
                    busy = False
                if not busy:
                    stop.wait(self.settings.poll_seconds)
        finally:
            self.queue.stop_worker(self.worker_id)
            _log.info("lab_worker.stopped", worker_id=self.worker_id)


def build_worker(settings: Settings, *, worker_id: str | None = None) -> LabWorker:
    """A worker over ``settings``' state DB and snapshot folder, with every
    job handler the API registers (one source of truth for what a job does)."""
    from stonks.app.services import Services
    from stonks.app.studio import user_strategies_dir
    from stonks.app.user_strategies import install

    offload = settings.lab.offload
    # The worker's own runner never offloads again.
    local = settings.model_copy(
        update={
            "lab": settings.lab.model_copy(
                update={"offload": offload.model_copy(update={"executor": "in_process"})}
            )
        }
    )
    snapshots = LakeSnapshots(
        offload.snapshot_root(settings.lake.path), keep=offload.keep_snapshots
    )
    context = SnapshotContext(local, snapshots)
    context.start()
    services = Services.create(context)
    if local.api.allow_code_strategies:
        install(user_strategies_dir(local))
    cpus = local.lab.parallel.resolved_workers()
    return LabWorker(
        offload,
        runner=services.runner,
        context=context,
        queue=LabQueue(settings.state.path),
        worker_id=worker_id,
        cpus=cpus,
    )
