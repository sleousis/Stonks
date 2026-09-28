"""A lab worker on another machine: ``python -m stonks.lab.offload worker
--api URL`` (roadmap 14.9).

It never opens the server's state DB or lake. Everything goes through
``/api/lab/worker/*`` (:mod:`stonks.app.lab_workers`) with a ``lab_worker``
token from ``STONKS_LAB_WORKER_TOKEN``:

1. claim the oldest portable lab job (lab runs and sweeps that name no
   registered strategy), with the snapshot to read and the research seed;
2. download that snapshot once into ``<work dir>/lab_snapshots`` and keep
   it until a claim names a newer one;
3. run the job with the API's own handler on scratch stores under
   ``<work dir>/jobs/<job id>``: a state DB seeded with the trial ledger
   and the active strategies, and an artifacts folder;
4. heartbeat all along (progress in, cancel out); a stop (SIGTERM) hands
   the job back to the queue;
5. upload the outcome with the ledger rows, trial matrices and registered
   strategies the job wrote, then delete the scratch stores.
"""

from __future__ import annotations

import os
import shutil
import socket
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from stonks.app.jobs import JobContext, JobStore
from stonks.config import Settings
from stonks.lab.offload.api_client import (
    LabWorkerApiClient,
    LabWorkerApiError,
    LabWorkerApiUnavailable,
)
from stonks.lab.offload.research_sync import ResearchRows, apply_rows, collect_delta
from stonks.lab.offload.snapshot import LakeSnapshots, SnapshotInfo
from stonks.lab.offload.transfer import install_snapshot
from stonks.lab.offload.worker import SnapshotContext
from stonks.logging import get_logger
from stonks.store.state import SqliteState

_log = get_logger("stonks.lab.offload.remote_worker")

#: Tries (and the back-off between them) for the final upload of a job.
_COMPLETE_DELAYS: tuple[float, ...] = (1.0, 5.0, 15.0)


class _Stopped(Exception):
    """The job was stopped (worker shutdown, cancel, lost) before it ran."""


@contextmanager
def _data_dir(path: Path) -> Iterator[None]:
    """``STONKS_DATA_DIR`` points at the job's scratch stores while it runs,
    so code that loads the settings itself (the pool correlation test, the
    lab's spawned processes) reads the seeded state, never another one."""
    before = os.environ.get("STONKS_DATA_DIR")
    os.environ["STONKS_DATA_DIR"] = str(path)
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("STONKS_DATA_DIR", None)
        else:
            os.environ["STONKS_DATA_DIR"] = before


class _RemoteHeartbeat:
    """Beats for one claimed job, from the claim until the upload."""

    def __init__(self, worker: RemoteLabWorker, job_id: str, interval: float) -> None:
        self._worker = worker
        self._job_id = job_id
        self._interval = interval
        self._ctx: JobContext | None = None
        self._store: JobStore | None = None
        self.message: str | None = "preparing"
        self.lost = False
        self.cancel_requested = False
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name=f"lab-remote-heartbeat-{job_id}", daemon=True
        )

    def __enter__(self) -> _RemoteHeartbeat:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    @property
    def halted(self) -> bool:
        return self.lost or self.cancel_requested

    def attach(self, ctx: JobContext, store: JobStore) -> None:
        self._ctx, self._store = ctx, store
        if self.halted:
            ctx.request_cancel()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            self.beat()

    def _progress(self) -> tuple[float, str | None]:
        if self._store is None:
            return 0.0, self.message
        job = self._store.get(self._job_id)
        return job.progress, job.message

    def beat(self) -> None:
        worker = self._worker
        try:
            progress, message = self._progress()
            reply = worker.client.heartbeat(
                self._job_id, worker.worker_id, progress=progress, message=message
            )
        except Exception as exc:  # a missed beat is retried on the next one
            _log.warning("lab_worker.heartbeat_failed", job_id=self._job_id, error=str(exc))
            return
        if not reply.get("running", False):
            self.lost = True
        if reply.get("cancel_requested"):
            self.cancel_requested = True
        if self.halted and self._ctx is not None:
            self._ctx.request_cancel()


class RemoteLabWorker:
    def __init__(
        self,
        settings: Settings,
        client: LabWorkerApiClient,
        *,
        work_dir: str | Path,
        worker_id: str | None = None,
        cpus: int | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.work_dir = Path(work_dir)
        self.worker_id = worker_id or f"lw_{socket.gethostname()}_{uuid.uuid4().hex[:8]}"
        self.cpus = cpus or settings.lab.parallel.resolved_workers()
        self.snapshots = LakeSnapshots(
            self.work_dir / "lab_snapshots", keep=settings.lab.offload.keep_snapshots
        )
        self.config: dict[str, Any] | None = None
        self._current: JobContext | None = None
        self._stopping = False

    # ---- lifecycle ---------------------------------------------------------------

    def register(self) -> dict[str, Any]:
        if self.config is None:
            self.config = self.client.register(
                self.worker_id, host=socket.gethostname(), pid=os.getpid(), cpus=self.cpus
            )
            _log.info(
                "lab_worker.started",
                worker_id=self.worker_id,
                api=self.client.base_url,
                kinds=self.config.get("kinds"),
            )
        return self.config

    @property
    def poll_seconds(self) -> float:
        cfg = self.config or {}
        return float(cfg.get("poll_seconds") or self.settings.lab.offload.poll_seconds)

    @property
    def heartbeat_seconds(self) -> float:
        cfg = self.config or {}
        return float(cfg.get("heartbeat_seconds") or self.settings.lab.offload.heartbeat_seconds)

    def request_stop(self) -> None:
        """Stop the running job at its next checkpoint; it goes back to the queue."""
        self._stopping = True
        if self._current is not None:
            self._current.request_cancel()

    def stop(self) -> None:
        try:
            self.client.stop(self.worker_id)
        except LabWorkerApiError as exc:
            _log.warning("lab_worker.stop_failed", worker_id=self.worker_id, error=str(exc))
        _log.info("lab_worker.stopped", worker_id=self.worker_id)

    def run_once(self) -> str | None:
        """Claim and run one job; its id, or ``None`` when idle."""
        self.register()
        claim = self.client.claim(self.worker_id)
        if claim is None:
            return None
        self._run(claim)
        return str(claim["id"])

    def run_forever(self, stop: threading.Event) -> None:
        try:
            while not stop.is_set():
                try:
                    busy = self.run_once() is not None
                except LabWorkerApiError as exc:  # API down or restarting: poll again
                    _log.error("lab_worker.loop_error", error=str(exc))
                    busy = False
                except Exception as exc:
                    _log.error(
                        "lab_worker.loop_error", error=str(exc), error_type=type(exc).__name__
                    )
                    busy = False
                if not busy:
                    stop.wait(self.poll_seconds)
        finally:
            self.stop()

    # ---- one job -----------------------------------------------------------------

    def _run(self, claim: dict[str, Any]) -> None:
        job_id = str(claim["id"])
        log = _log.bind(job_id=job_id, worker_id=self.worker_id)
        log.info("lab_worker.job_started", kind=claim.get("kind"))
        job_dir = self.work_dir / "jobs" / job_id
        with _RemoteHeartbeat(self, job_id, self.heartbeat_seconds) as beat:
            try:
                info = self._ensure_snapshot(claim["snapshot"], beat)
                outcome = self._execute(claim, info, job_dir, beat)
            except _Stopped:
                outcome = None
            except Exception as exc:  # the snapshot or the scratch stores failed
                log.error("lab_worker.job_setup_failed", error=str(exc))
                self._complete(job_id, {"status": "failed", "error": f"lab worker: {exc}"}, log)
                shutil.rmtree(job_dir, ignore_errors=True)
                return
            if beat.lost:
                log.warning("lab_worker.job_lost", reason="the API no longer holds it for us")
            elif outcome is None or (
                outcome["status"] == "cancelled" and self._stopping and not beat.cancel_requested
            ):
                self._release(job_id, log)
            else:
                self._complete(job_id, outcome, log)
        shutil.rmtree(job_dir, ignore_errors=True)

    def _ensure_snapshot(self, ref: dict[str, Any], beat: _RemoteHeartbeat) -> SnapshotInfo:
        name = str(ref["name"])
        current = self.snapshots.current()
        if current is not None and current.directory.name == name:
            return current
        cached = self.snapshots.get(name)
        created = datetime.fromisoformat(str(ref["created_at"]))
        fingerprint = str(ref.get("fingerprint") or "")
        if cached is not None:
            return self.snapshots.adopt(name, created, fingerprint)
        beat.message = "downloading the lake snapshot"
        started = time.perf_counter()
        with self.client.snapshot(name) as chunks:
            info = install_snapshot(
                self.snapshots,
                name,
                self._until_stopped(chunks, beat),
                created_at=created,
                fingerprint=fingerprint,
            )
        _log.info(
            "lab_worker.snapshot_downloaded",
            snapshot=name,
            seconds=round(time.perf_counter() - started, 3),
        )
        return info

    def _until_stopped(self, chunks: Iterator[bytes], beat: _RemoteHeartbeat) -> Iterator[bytes]:
        for chunk in chunks:
            if self._stopping or beat.halted:
                raise _Stopped()
            yield chunk

    def _job_settings(self, job_dir: Path) -> Settings:
        s = self.settings
        offload = s.lab.offload.model_copy(
            update={"executor": "in_process", "snapshot_dir": self.snapshots.root}
        )
        return s.model_copy(
            update={
                "state": s.state.model_copy(update={"path": job_dir / "state.sqlite"}),
                "lake": s.lake.model_copy(update={"path": job_dir / "lake.duckdb"}),
                "registry": s.registry.model_copy(update={"artifacts_dir": job_dir / "artifacts"}),
                "lab": s.lab.model_copy(update={"offload": offload}),
            }
        )

    def _execute(
        self,
        claim: dict[str, Any],
        info: SnapshotInfo,
        job_dir: Path,
        beat: _RemoteHeartbeat,
    ) -> dict[str, Any] | None:
        from stonks.app.services import Services

        if self._stopping or beat.halted:
            raise _Stopped()
        job_id = str(claim["id"])
        shutil.rmtree(job_dir, ignore_errors=True)
        job_dir.mkdir(parents=True)
        local = self._job_settings(job_dir)
        seed = ResearchRows.model_validate(claim.get("seed") or {})
        with SqliteState(local.state.path) as state:
            state.migrate()
            apply_rows(state, local.registry.artifacts_dir, seed, keep_status=True)
        store = JobStore(local.state.path)
        store.adopt(
            job_id,
            str(claim["kind"]),
            dict(claim.get("params") or {}),
            owner_id=claim.get("owner_id"),
            created_at=datetime.fromisoformat(str(claim["created_at"])),
        )
        context = SnapshotContext(local, self.snapshots)
        context.start()
        services = Services.create(context)
        ctx = JobContext(job_id=job_id, _store=services.runner.store)
        beat.attach(ctx, services.runner.store)
        self._current = ctx
        if self._stopping:
            ctx.request_cancel()
        try:
            with _data_dir(job_dir), context.pinned(info):
                status = services.runner.run_claimed(job_id, ctx)
        finally:
            self._current = None
            services.shutdown(wait=True)
        job = store.get(job_id)
        with SqliteState(local.state.path) as state:
            research = collect_delta(state, local.registry.artifacts_dir, seed=seed)
        return {
            "status": job.status if job.is_terminal else status,
            "result": job.result,
            "error": job.error,
            "research": research.model_dump(mode="json"),
        }

    def _complete(self, job_id: str, outcome: dict[str, Any], log: Any) -> None:
        body = {"worker_id": self.worker_id, **outcome}
        for attempt, delay in enumerate((*_COMPLETE_DELAYS, None)):
            try:
                self.client.complete(job_id, body)
                log.info("lab_worker.job_finished", outcome=outcome["status"])
                return
            except LabWorkerApiUnavailable as exc:
                if delay is None:
                    log.error("lab_worker.complete_failed", error=str(exc), attempts=attempt + 1)
                    return
                time.sleep(delay)
            except LabWorkerApiError as exc:  # 409: reaped or cancelled meanwhile
                log.error("lab_worker.complete_refused", error=str(exc), status=exc.status)
                return

    def _release(self, job_id: str, log: Any) -> None:
        try:
            job = self.client.release(job_id, self.worker_id)
            log.info("lab_worker.job_requeued", status=job.get("status"), reason="worker stopping")
        except LabWorkerApiError as exc:
            log.error("lab_worker.release_failed", error=str(exc))


def default_work_dir(settings: Settings) -> Path:
    """``<data dir>/lab_worker``: snapshots and scratch job stores."""
    return Path(settings.lake.path).parent / "lab_worker"


def build_remote_worker(
    settings: Settings,
    *,
    api_url: str,
    token: str,
    worker_id: str | None = None,
    work_dir: str | Path | None = None,
    trusted_hosts: tuple[str, ...] = (),
    http: Any = None,
) -> RemoteLabWorker:
    client = LabWorkerApiClient(api_url, token=token, trusted_hosts=trusted_hosts, http=http)
    return RemoteLabWorker(
        settings,
        client,
        work_dir=work_dir or default_work_dir(settings),
        worker_id=worker_id,
    )
