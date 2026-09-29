"""Background jobs: a SQLite-backed store plus a small in-process runner.

Long operations (backtests, lab runs, ingest, production ticks) are
submitted as jobs so transports can return immediately and poll or stream
status. Every job is a row in the ``jobs`` table (state migration 003); the
row is the source of truth, the runner only holds futures for waiting.

Concurrency model
-----------------
- **Lanes.** Unlocked jobs (backtests, lab runs) share a general
  :class:`~concurrent.futures.ThreadPoolExecutor` of ``max_workers``.
  Every lock name (``"lake_write"`` for ingests, ``"tick"`` for ticks) gets
  its own single-worker executor, so jobs sharing a lock run one at a time
  *without* occupying a general worker while they wait, and a tick never
  waits behind ingests or long lab runs. Total threads are
  ``max_workers + number of lock names``.
- **DuckDB**: a ``DuckDBPyConnection`` must not be shared across threads,
  but several connections to the same database file in one process are
  fine — DuckDB keeps one database instance per path per process and each
  ``duckdb.connect(path)`` opens a new connection onto it with MVCC
  isolation. So handlers never receive a connection: each one opens its
  own ``DuckDBLake`` inside its worker thread and closes it when done.
  Concurrent writers to the same rows would hit DuckDB's optimistic
  write-write conflict errors, which is why lake writers share the
  ``lake_write`` lane. Only one *process* can hold the lake read-write, so
  the CLI cannot write the lake while ``stonks serve`` is running.
- **SQLite**: every store call opens a short-lived connection (WAL mode,
  sqlite3's default 5 s busy timeout); status transitions are single
  guarded ``UPDATE ... WHERE status = ?`` statements, so they are atomic
  without an in-process lock. Claim and finish writes that still hit a
  SQLite error are retried with back-off; if the final write keeps failing
  the error is logged, the row stays non-terminal and the runner stops
  tracking it (:meth:`JobRunner.is_tracked`), which is what ends event
  streams and :meth:`JobRunner.wait`.
- A handler that raises marks its job ``failed``; jobs still ``queued`` or
  ``running`` when the process died are marked ``failed`` by
  :meth:`JobStore.recover_interrupted` at the next startup.
- **Cancellation.** A ``queued`` job can always be cancelled (it never
  runs). A ``running`` job can be cancelled only when its kind was
  registered ``cancellable`` (lab runs): cancellation is cooperative — the
  handler checks :meth:`JobContext.check_cancelled` between tuning trials
  and survival tests. Ticks and ingests are never interrupted mid-way (a
  half-applied tick is worse than a finished one).
- **Shutdown** cancels queued jobs and asks cancellable running jobs to
  stop; other running jobs finish. See :meth:`JobRunner.shutdown` for why
  ``wait=False`` does not make the process exit earlier.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from stonks.app.errors import AppError, ConflictError, NotFoundError, ValidationError
from stonks.app.pagination import Page
from stonks.app.serialize import to_jsonable
from stonks.ingest.redact import format_exception, redact_secrets
from stonks.lab.offload.executor import InProcessLabExecutor, LabExecutor
from stonks.logging import get_logger
from stonks.store.state import SqliteState

JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]
TERMINAL_STATUSES: frozenset[str] = frozenset({"succeeded", "failed", "cancelled"})

_log = get_logger("stonks.app.jobs")


class Job(BaseModel):
    id: str
    kind: str
    params: dict[str, Any]
    status: JobStatus
    progress: float
    message: str | None = None
    #: Untyped here (it depends on ``kind``); each kind has a typed
    #: ``.../{job_id}/result`` route, e.g. ``GET /api/lab/backtests/{id}/result``.
    result: Any = None
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    #: Who queued it (``null`` for the scheduler and other services). Only
    #: the owner and admins see a job.
    owner_id: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


class JobStore:
    """CRUD + guarded status transitions over the ``jobs`` table."""

    def __init__(self, state_path: str | Path) -> None:
        self._path = Path(state_path)

    def _state(self) -> SqliteState:
        return SqliteState(self._path)

    # ---- writes ------------------------------------------------------------

    def create(
        self,
        kind: str,
        params: dict[str, Any],
        *,
        owner_id: str | None = None,
        executor: str = "local",
    ) -> Job:
        """A new ``queued`` row. ``executor="worker"`` leaves it for a lab
        worker process (roadmap 14.9, ``stonks.lab.offload``)."""
        job_id = f"job_{uuid.uuid4().hex}"
        with self._state() as s:
            s.execute(
                "INSERT INTO jobs (id, kind, params_json, status, progress, created_at, owner_id,"
                " executor) VALUES (?, ?, ?, 'queued', 0, ?, ?, ?)",
                [
                    job_id,
                    kind,
                    json.dumps(to_jsonable(params), sort_keys=True),
                    _now(),
                    owner_id,
                    executor,
                ],
            )
        return self.get(job_id)

    def adopt(
        self,
        job_id: str,
        kind: str,
        params: dict[str, Any],
        *,
        owner_id: str | None,
        created_at: datetime,
    ) -> Job:
        """A ``running`` copy of a job claimed on another server, under its
        own id: a remote lab worker runs it here through
        :meth:`JobRunner.run_claimed` and reports the outcome back."""
        now = _now()
        with self._state() as s:
            s.execute(
                "INSERT INTO jobs (id, kind, params_json, status, progress, created_at,"
                " started_at, owner_id) VALUES (?, ?, ?, 'running', 0, ?, ?, ?)",
                [
                    job_id,
                    kind,
                    json.dumps(to_jsonable(params), sort_keys=True),
                    created_at.astimezone(UTC).isoformat(timespec="microseconds"),
                    now,
                    owner_id,
                ],
            )
        return self.get(job_id)

    def claim(self, job_id: str) -> bool:
        """``queued`` → ``running``; False when the job was cancelled first."""
        with self._state() as s:
            cur = s.execute(
                "UPDATE jobs SET status='running', started_at=? WHERE id=? AND status='queued'",
                [_now(), job_id],
            )
            return cur.rowcount == 1

    def set_progress(self, job_id: str, progress: float, message: str | None = None) -> None:
        clamped = min(1.0, max(0.0, float(progress)))
        with self._state() as s:
            s.execute(
                "UPDATE jobs SET progress=?, progress_message=? WHERE id=? AND status='running'",
                [clamped, message, job_id],
            )

    def finish(
        self,
        job_id: str,
        status: Literal["succeeded", "failed", "cancelled"],
        *,
        result: Any = None,
        error: str | None = None,
    ) -> None:
        result_json = None if result is None else json.dumps(to_jsonable(result), allow_nan=False)
        progress_sql = ", progress=1" if status == "succeeded" else ""
        with self._state() as s:
            s.execute(
                f"UPDATE jobs SET status=?, result_json=?, error=?, finished_at=?{progress_sql} "
                "WHERE id=? AND status IN ('queued', 'running')",
                [status, result_json, error, _now(), job_id],
            )

    def cancel(self, job_id: str) -> Job:
        with self._state() as s:
            cur = s.execute(
                "UPDATE jobs SET status='cancelled', finished_at=? WHERE id=? AND status='queued'",
                [_now(), job_id],
            )
            changed = cur.rowcount == 1
        job = self.get(job_id)
        if not changed:
            if job.status == "running":
                raise ConflictError(f"job {job_id} is running and cannot be cancelled")
            raise ConflictError(f"job {job_id} already finished ({job.status})")
        return job

    def recover_interrupted(self) -> int:
        """Mark jobs left ``queued``/``running`` by a dead process as failed.
        Worker jobs are left alone: a lab worker owns them (it fails its
        own lost jobs through the heartbeat lease)."""
        with self._state() as s:
            cur = s.execute(
                "UPDATE jobs SET status='failed', finished_at=?, "
                "error='interrupted: the server stopped before the job finished' "
                "WHERE status IN ('queued', 'running') AND executor = 'local'",
                [_now()],
            )
            return cur.rowcount

    # ---- reads -------------------------------------------------------------

    def get(self, job_id: str) -> Job:
        with self._state() as s:
            rows = s.sql("SELECT * FROM jobs WHERE id=?", [job_id])
        if not rows:
            raise NotFoundError(f"no job with id {job_id!r}")
        return _row_to_job(rows[0])

    def executor_of(self, job_id: str) -> str:
        """Who runs the job: ``"local"`` (this process) or ``"worker"``."""
        with self._state() as s:
            rows = s.sql("SELECT executor FROM jobs WHERE id=?", [job_id])
        if not rows:
            raise NotFoundError(f"no job with id {job_id!r}")
        return str(rows[0]["executor"])

    def pending(self, kinds: Iterable[str]) -> list[Job]:
        """Queued or running jobs of ``kinds``, oldest first."""
        wanted = sorted(set(kinds))
        if not wanted:
            return []
        marks = ", ".join("?" * len(wanted))
        with self._state() as s:
            rows = s.sql(
                f"SELECT * FROM jobs WHERE kind IN ({marks}) AND status IN ('queued', 'running')"
                " ORDER BY created_at, rowid",
                wanted,
            )
        return [_row_to_job(r) for r in rows]

    def list(
        self,
        *,
        status: str | None = None,
        kind: str | None = None,
        owner_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Page[Job]:
        where: list[str] = []
        params: list[Any] = []
        if owner_id is not None:
            where.append("owner_id = ?")
            params.append(owner_id)
        if status is not None:
            where.append("status = ?")
            params.append(status)
        if kind is not None:
            where.append("kind = ?")
            params.append(kind)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        with self._state() as s:
            total = int(s.sql(f"SELECT COUNT(*) FROM jobs{clause}", params)[0][0])
            rows = s.sql(
                f"SELECT * FROM jobs{clause} ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            )
        return Page[Job](
            items=[_row_to_job(r) for r in rows], total=total, limit=limit, offset=offset
        )


class JobCancelled(BaseException):  # a signal, not an error
    """Raised by :meth:`JobContext.check_cancelled` once cancellation of a
    running job was requested; the runner records the job ``cancelled``.

    A ``BaseException`` (like ``KeyboardInterrupt``) on purpose: the tuners
    catch ``Exception`` per trial to keep going, and must not swallow it."""


@dataclass
class JobContext:
    """Handed to every handler: progress reporting and cooperative
    cancellation checkpoints."""

    job_id: str
    _store: JobStore
    _cancel: threading.Event = field(default_factory=threading.Event)

    def progress(self, fraction: float, message: str | None = None) -> None:
        """Best effort: a failed progress write never fails the job."""
        try:
            self._store.set_progress(self.job_id, fraction, message)
        except Exception as exc:
            _log.warning("job.progress_write_failed", job_id=self.job_id, error=str(exc))

    def request_cancel(self) -> None:
        self._cancel.set()

    @property
    def cancel_requested(self) -> bool:
        return self._cancel.is_set()

    def check_cancelled(self) -> None:
        """Raise :class:`JobCancelled` when cancellation was requested.
        Long handlers call this at safe points (e.g. between tuning trials)."""
        if self._cancel.is_set():
            raise JobCancelled(f"job {self.job_id} was cancelled")


JobHandler = Callable[[dict[str, Any], JobContext], Any]


@dataclass(frozen=True)
class _Registration:
    handler: JobHandler
    lock: str | None
    cancellable: bool
    operation: bool


#: Back-off between retries of a job-row write that hit a SQLite error
#: (typically ``database is locked`` past the 5 s busy timeout).
DEFAULT_RETRY_DELAYS: tuple[float, ...] = (0.1, 0.5, 2.0)


class JobRunner:
    def __init__(
        self,
        store: JobStore,
        max_workers: int = 2,
        *,
        secrets: Callable[[], Iterable[str]] = tuple,
        retry_delays: Sequence[float] = DEFAULT_RETRY_DELAYS,
        lab_executor: LabExecutor | None = None,
    ) -> None:
        """``max_workers`` bounds the general pool (unlocked jobs); every
        lock name gets one extra dedicated worker (see the module doc).
        ``secrets`` returns credential values scrubbed from every stored and
        logged job error (on top of generic ``token=...`` patterns).
        ``lab_executor`` decides which jobs a lab worker process runs
        instead (roadmap 14.9); the default runs everything here."""
        self._store = store
        self._lab_executor: LabExecutor = lab_executor or InProcessLabExecutor()
        self.secrets = secrets
        self._retry_delays = tuple(retry_delays)
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="stonks-job"
        )
        self._lanes: dict[str, ThreadPoolExecutor] = {}
        self._handlers: dict[str, _Registration] = {}
        self._futures: dict[str, Future[None]] = {}
        self._contexts: dict[str, JobContext] = {}
        self._guard = threading.Lock()
        self._stopping = threading.Event()

    @property
    def store(self) -> JobStore:
        return self._store

    @property
    def lab_executor(self) -> LabExecutor:
        return self._lab_executor

    def register(
        self,
        kind: str,
        handler: JobHandler,
        *,
        lock: str | None = None,
        cancellable: bool = False,
        operation: bool | None = None,
    ) -> None:
        """Register ``handler`` for ``kind``. Jobs sharing a ``lock`` name
        run one at a time on that lock's own worker (e.g. every lake writer
        uses ``"lake_write"``). ``cancellable`` handlers call
        :meth:`JobContext.check_cancelled`, so a running job can be stopped.
        ``operation`` marks operator jobs (ticks, ingests, backups) whose
        cancel needs an admin; it defaults to "holds a lock lane", and
        research jobs on a lane (universe refresh) pass ``False``."""
        with self._guard:
            self._handlers[kind] = _Registration(
                handler=handler,
                lock=lock,
                cancellable=cancellable,
                operation=lock is not None if operation is None else operation,
            )
            if lock is not None and lock not in self._lanes:
                self._lanes[lock] = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix=f"stonks-job-{lock}"
                )

    @property
    def kinds(self) -> list[str]:
        return sorted(self._handlers)

    def submit(self, kind: str, params: dict[str, Any], *, owner_id: str | None = None) -> Job:
        if self._stopping.is_set():
            raise ConflictError("job runner is shutting down")
        reg = self._handlers.get(kind)
        if reg is None:
            raise ValidationError(f"unknown job kind {kind!r}; known: {self.kinds}")
        if self._lab_executor.offloads(kind, params):
            return self._offload(kind, params, owner_id)
        job = self._store.create(kind, params, owner_id=owner_id)
        ctx = JobContext(job_id=job.id, _store=self._store)
        executor = self._executor if reg.lock is None else self._lanes[reg.lock]
        try:
            with self._guard:
                future = executor.submit(self._run, job.id, kind, job.params, ctx)
                self._futures[job.id] = future
                self._contexts[job.id] = ctx
        except RuntimeError:  # executor shut down between the check and here
            self._finish_safely(job.id, "cancelled", error="server shut down before start")
            raise ConflictError("job runner is shutting down") from None
        # Outside the guard: a future that already finished runs the
        # callback right here, and _forget takes the guard.
        future.add_done_callback(lambda _f, jid=job.id: self._forget(jid))
        _log.info("job.submitted", job_id=job.id, kind=kind, lane=reg.lock or "general")
        return job

    def _offload(self, kind: str, params: dict[str, Any], owner_id: str | None) -> Job:
        """Queue ``kind`` for the lab executor; this process never runs it."""
        executor = self._lab_executor
        executor.prepare(kind, params)
        job = self._store.create(kind, params, owner_id=owner_id, executor=executor.name)
        _log.info("job.submitted", job_id=job.id, kind=kind, lane=f"executor:{executor.name}")
        return job

    def run_claimed(self, job_id: str, ctx: JobContext | None = None) -> JobStatus:
        """Run a job another component already moved to ``running`` (a lab
        worker's claim), here and now, and return its final status. Errors,
        cancellation and result storage work exactly as for submitted jobs."""
        job = self._store.get(job_id)
        ctx = ctx or JobContext(job_id=job_id, _store=self._store)
        log = _log.bind(job_id=job_id, kind=job.kind)
        if job.kind not in self._handlers:
            self._finish_safely(job_id, "failed", error=f"unknown job kind {job.kind!r}")
        else:
            try:
                self._execute(job_id, job.kind, job.params, ctx, log, claimed=True)
            except BaseException as exc:  # same last resort as _run
                message = format_exception(exc, self.secrets())
                log.error("job.runner_error", error=message, error_type=type(exc).__name__)
                self._finish_safely(job_id, "failed", error=message)
        final = self._store.get(job_id)
        return final.status if final.is_terminal else "failed"

    def run_in_lane[T](self, lane: str, fn: Callable[[], T], *, timeout: float = 30.0) -> T:
        """Run a short write ``fn`` on ``lane``'s worker and wait for it, so
        it never overlaps the lane's jobs (e.g. a universe delete beside a
        refresh on ``lake_write``). ``ConflictError`` when the lane stays
        busy for ``timeout`` seconds; the write is then dropped, never run
        later. Errors raised by ``fn`` propagate."""
        if self._stopping.is_set():
            raise ConflictError("job runner is shutting down")
        with self._guard:
            executor = self._lanes.get(lane)
            if executor is None:
                executor = self._lanes[lane] = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix=f"stonks-job-{lane}"
                )
        future = executor.submit(fn)
        try:
            return future.result(timeout=timeout)
        except FutureTimeout:
            if future.cancel():
                raise ConflictError(
                    f"the {lane} lane is busy with a running job; try again shortly"
                ) from None
            return future.result()  # it started just now: let it finish

    def lane(self, kind: str) -> str | None:
        """The lock lane ``kind`` runs on, or ``None`` for the general pool."""
        reg = self._handlers.get(kind)
        return reg.lock if reg is not None else None

    def is_operation(self, kind: str) -> bool:
        """True for operator jobs (ticks, ingests, backups), whose cancel
        needs an admin (see :meth:`register`)."""
        reg = self._handlers.get(kind)
        return reg is not None and reg.operation

    def cancel(self, job_id: str) -> Job:
        """Cancel a queued job, or request cancellation of a running job whose
        kind is ``cancellable`` (it stops at its next checkpoint and ends
        ``cancelled``). Anything else is a :class:`ConflictError`."""
        job = self._store.get(job_id)
        if job.status == "queued":
            try:
                return self._store.cancel(job_id)
            except ConflictError:
                job = self._store.get(job_id)  # claimed in the meantime
        if job.status == "running":
            reg = self._handlers.get(job.kind)
            with self._guard:
                ctx = self._contexts.get(job_id)
            if reg is not None and reg.cancellable and ctx is not None:
                ctx.request_cancel()
                ctx.progress(job.progress, "cancellation requested")
                _log.info("job.cancel_requested", job_id=job_id, kind=job.kind)
                return self._store.get(job_id)
            if reg is not None and reg.cancellable and self._lab_executor.request_cancel(job_id):
                self._store.set_progress(job_id, job.progress, "cancellation requested")
                _log.info("job.cancel_requested", job_id=job_id, kind=job.kind, lane="worker")
                return self._store.get(job_id)
            raise ConflictError(f"job {job_id} is running and cannot be cancelled")
        raise ConflictError(f"job {job_id} already finished ({job.status})")

    def is_tracked(self, job_id: str) -> bool:
        """True while this runner still holds the job (queued or running).
        A non-terminal job that is not tracked will never be updated by this
        process (e.g. its final status write kept failing)."""
        with self._guard:
            if job_id in self._futures:
                return True
        try:
            return self._lab_executor.tracks(job_id)
        except Exception:
            return False

    def wait(self, job_id: str, timeout: float | None = None) -> Job:
        """Block until the job is terminal (or ``timeout`` elapses). Returns
        early, non-terminal, when this runner no longer tracks the job."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._guard:
            future = self._futures.get(job_id)
        if future is not None:
            try:
                future.result(timeout=timeout)
            except FutureTimeout:
                return self._store.get(job_id)
            except Exception:  # _run never raises; cancelled futures do
                pass
            # The handler is done: whatever the row says now is final for
            # this process (a failed status write stays non-terminal).
            return self._store.get(job_id)
        job = self._store.get(job_id)
        while not job.is_terminal:
            if deadline is None:
                # Only an offloaded job can still finish: a worker runs it.
                if not self._lab_executor.tracks(job_id):
                    # The worker may have finished it since the last read.
                    job = self._store.get(job_id)
                    break
                time.sleep(0.05)
            elif time.monotonic() < deadline:
                time.sleep(0.02)
            else:
                break
            job = self._store.get(job_id)
        return job

    def shutdown(self, wait: bool = True) -> None:
        """Stop accepting work.

        - Jobs that never started are cancelled.
        - Running ``cancellable`` jobs (lab runs) are asked to stop and end
          ``cancelled`` at their next checkpoint.
        - Other running jobs (ticks, ingests, backtests) run to completion.

        ``wait=False`` returns without joining workers, but it cannot make
        the process exit sooner: ``concurrent.futures`` joins every worker
        thread at interpreter exit, so the process lives until the running
        jobs return. A job still running when the process is killed is
        marked failed by :meth:`JobStore.recover_interrupted` at the next
        startup.
        """
        self._stopping.set()
        with self._guard:
            pending = dict(self._futures)
            contexts = dict(self._contexts)
        for job_id, future in pending.items():
            if future.cancel():
                self._finish_safely(job_id, "cancelled", error="server shut down before start")
            elif job_id in contexts:
                reg = self._registration_for(job_id)
                if reg is not None and reg.cancellable:
                    contexts[job_id].request_cancel()
        executors = [self._executor, *self._lanes.values()]
        for executor in executors:
            executor.shutdown(wait=False, cancel_futures=True)
        if wait:
            for executor in executors:
                executor.shutdown(wait=True)

    # ---- internals ---------------------------------------------------------

    def _registration_for(self, job_id: str) -> _Registration | None:
        try:
            return self._handlers.get(self._store.get(job_id).kind)
        except Exception:
            return None

    def _forget(self, job_id: str) -> None:
        with self._guard:
            self._futures.pop(job_id, None)
            self._contexts.pop(job_id, None)

    def _retrying[T](self, op: str, job_id: str, fn: Callable[[], T]) -> T:
        """Run a job-row write, retrying SQLite errors with back-off."""
        for attempt, delay in enumerate((*self._retry_delays, None)):
            try:
                return fn()
            except sqlite3.Error as exc:
                if delay is None:
                    raise
                _log.warning(
                    "job.store_retry", job_id=job_id, op=op, attempt=attempt + 1, error=str(exc)
                )
                time.sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    def _finish_safely(
        self,
        job_id: str,
        status: Literal["succeeded", "failed", "cancelled"],
        *,
        result: Any = None,
        error: str | None = None,
    ) -> bool:
        """Last-resort terminal write: retried, and never raises. When it
        still fails the row stays non-terminal until the next startup's
        :meth:`JobStore.recover_interrupted`; streams end on their own
        because the runner stops tracking the job."""
        try:
            self._retrying(
                "finish",
                job_id,
                lambda: self._store.finish(job_id, status, result=result, error=error),
            )
            return True
        except Exception as exc:
            _log.error(
                "job.finish_failed",
                job_id=job_id,
                status=status,
                error=redact_secrets(str(exc), self.secrets()),
                error_type=type(exc).__name__,
            )
            return False

    def _run(self, job_id: str, kind: str, params: dict[str, Any], ctx: JobContext) -> None:
        log = _log.bind(job_id=job_id, kind=kind)
        try:
            self._execute(job_id, kind, params, ctx, log)
        except BaseException as exc:  # last resort: never leave a job silently stuck
            message = format_exception(exc, self.secrets())
            log.error("job.runner_error", error=message, error_type=type(exc).__name__)
            self._finish_safely(job_id, "failed", error=message)

    def _execute(
        self,
        job_id: str,
        kind: str,
        params: dict[str, Any],
        ctx: JobContext,
        log: Any,
        *,
        claimed: bool = False,
    ) -> None:
        reg = self._handlers[kind]
        # A job still queued in its lane at shutdown must not start.
        if self._stopping.is_set():
            self._finish_safely(job_id, "cancelled", error="server shut down before start")
            return
        if not claimed and not self._retrying("claim", job_id, lambda: self._store.claim(job_id)):
            log.info("job.skipped", reason="not queued (cancelled)")
            return
        log.info("job.started")
        try:
            result = reg.handler(params, ctx)
        except JobCancelled:
            log.info("job.cancelled_while_running")
            self._finish_safely(job_id, "cancelled", error="cancelled while running")
            return
        except AppError as exc:
            message = redact_secrets(str(exc), self.secrets())
            log.warning("job.failed", error=message, error_type=type(exc).__name__)
            self._finish_safely(job_id, "failed", error=message)
            return
        except Exception as exc:
            message = format_exception(exc, self.secrets())
            log.error("job.crashed", error=message, error_type=type(exc).__name__)
            self._finish_safely(job_id, "failed", error=message)
            return
        try:
            self._retrying(
                "finish",
                job_id,
                lambda: self._store.finish(job_id, "succeeded", result=result),
            )
        except sqlite3.Error as exc:
            log.error("job.finish_failed", status="succeeded", error=str(exc))
            return
        except Exception as exc:  # e.g. unserializable result
            log.error("job.result_unstorable", error=str(exc))
            self._finish_safely(
                job_id, "failed", error=f"could not store result: {type(exc).__name__}"
            )
            return
        log.info("job.succeeded")


def _row_to_job(row: Any) -> Job:
    return Job(
        id=row["id"],
        kind=row["kind"],
        params=json.loads(row["params_json"]),
        status=row["status"],
        progress=float(row["progress"]),
        message=row["progress_message"],
        result=None if row["result_json"] is None else json.loads(row["result_json"]),
        error=row["error"],
        created_at=datetime.fromisoformat(row["created_at"]),
        started_at=_parse_ts(row["started_at"]),
        finished_at=_parse_ts(row["finished_at"]),
        owner_id=row["owner_id"],
    )


def _parse_ts(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")
