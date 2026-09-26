"""Background jobs: a SQLite-backed store plus a small in-process runner.

Long operations (backtests, lab runs, ingest, production ticks) are
submitted as jobs so transports can return immediately and poll or stream
status. Every job is a row in the ``jobs`` table (state migration 003); the
row is the source of truth, the runner only holds futures for waiting.

Concurrency model
-----------------
- A bounded :class:`~concurrent.futures.ThreadPoolExecutor` runs handlers;
  ``max_workers`` caps how many jobs run at once.
- **DuckDB**: a ``DuckDBPyConnection`` must not be shared across threads,
  but several connections to the same database file in one process are
  fine — DuckDB keeps one database instance per path per process and each
  ``duckdb.connect(path)`` opens a new connection onto it with MVCC
  isolation. So handlers never receive a connection: each one opens its
  own ``DuckDBLake`` inside its worker thread and closes it when done.
  Concurrent writers to the same rows would hit DuckDB's optimistic
  write-write conflict errors, so handlers that write the lake register
  with ``lock="lake_write"`` and run one at a time. Only one *process* can
  hold the lake read-write, so the CLI cannot write the lake while
  ``stonks serve`` is running.
- **SQLite**: every store call opens a short-lived connection (WAL mode,
  sqlite3's default 5 s busy timeout); status transitions are single
  guarded ``UPDATE ... WHERE status = ?`` statements, so they are atomic
  without an in-process lock.
- A handler that raises marks its job ``failed``; jobs still ``queued`` or
  ``running`` when the process died are marked ``failed`` by
  :meth:`JobStore.recover_interrupted` at the next startup.
- Cancellation is honest: a ``queued`` job can be cancelled (it never
  runs); a ``running`` one cannot, because none of the wrapped operations
  can be interrupted safely mid-way (a half-applied tick is worse than a
  finished one).
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from stonks.app.errors import AppError, ConflictError, NotFoundError, ValidationError
from stonks.app.pagination import Page
from stonks.app.serialize import to_jsonable
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
    result: Any = None
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

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

    def create(self, kind: str, params: dict[str, Any]) -> Job:
        job_id = f"job_{uuid.uuid4().hex}"
        with self._state() as s:
            s.execute(
                "INSERT INTO jobs (id, kind, params_json, status, progress, created_at) "
                "VALUES (?, ?, ?, 'queued', 0, ?)",
                [job_id, kind, json.dumps(to_jsonable(params), sort_keys=True), _now()],
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
        """Mark jobs left ``queued``/``running`` by a dead process as failed."""
        with self._state() as s:
            cur = s.execute(
                "UPDATE jobs SET status='failed', finished_at=?, "
                "error='interrupted: the server stopped before the job finished' "
                "WHERE status IN ('queued', 'running')",
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

    def list(
        self,
        *,
        status: str | None = None,
        kind: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Page[Job]:
        where: list[str] = []
        params: list[Any] = []
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
                f"SELECT * FROM jobs{clause} ORDER BY created_at DESC, rowid DESC "
                "LIMIT ? OFFSET ?",
                [*params, limit, offset],
            )
        return Page[Job](
            items=[_row_to_job(r) for r in rows], total=total, limit=limit, offset=offset
        )


@dataclass
class JobContext:
    """Handed to every handler so it can report progress."""

    job_id: str
    _store: JobStore

    def progress(self, fraction: float, message: str | None = None) -> None:
        self._store.set_progress(self.job_id, fraction, message)


JobHandler = Callable[[dict[str, Any], JobContext], Any]


@dataclass(frozen=True)
class _Registration:
    handler: JobHandler
    lock: str | None


class JobRunner:
    def __init__(self, store: JobStore, max_workers: int = 2) -> None:
        self._store = store
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="stonks-job"
        )
        self._handlers: dict[str, _Registration] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._futures: dict[str, Future[None]] = {}
        self._guard = threading.Lock()

    @property
    def store(self) -> JobStore:
        return self._store

    def register(self, kind: str, handler: JobHandler, *, lock: str | None = None) -> None:
        """Register ``handler`` for ``kind``. Jobs sharing a ``lock`` name
        run one at a time (e.g. every lake writer uses ``"lake_write"``)."""
        with self._guard:
            self._handlers[kind] = _Registration(handler=handler, lock=lock)
            if lock is not None:
                self._locks.setdefault(lock, threading.Lock())

    @property
    def kinds(self) -> list[str]:
        return sorted(self._handlers)

    def submit(self, kind: str, params: dict[str, Any]) -> Job:
        if kind not in self._handlers:
            raise ValidationError(f"unknown job kind {kind!r}; known: {self.kinds}")
        job = self._store.create(kind, params)
        future = self._executor.submit(self._run, job.id, kind, job.params)
        with self._guard:
            self._futures[job.id] = future
        future.add_done_callback(lambda _f, jid=job.id: self._forget(jid))
        _log.info("job.submitted", job_id=job.id, kind=kind)
        return job

    def wait(self, job_id: str, timeout: float | None = None) -> Job:
        """Block until the job is terminal (or ``timeout`` elapses)."""
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
        job = self._store.get(job_id)
        while not job.is_terminal and (deadline is None or time.monotonic() < deadline):
            time.sleep(0.02)
            job = self._store.get(job_id)
        return job

    def shutdown(self, wait: bool = True) -> None:
        """Stop accepting work. Jobs that never started are cancelled; a
        running job left behind (``wait=False``) is marked failed by
        :meth:`JobStore.recover_interrupted` at the next startup."""
        with self._guard:
            pending = dict(self._futures)
        for job_id, future in pending.items():
            if future.cancel():
                self._store.finish(job_id, "cancelled", error="server shut down before start")
        self._executor.shutdown(wait=wait, cancel_futures=True)

    # ---- internals ---------------------------------------------------------

    def _forget(self, job_id: str) -> None:
        with self._guard:
            self._futures.pop(job_id, None)

    def _run(self, job_id: str, kind: str, params: dict[str, Any]) -> None:
        reg = self._handlers[kind]
        lock = self._locks[reg.lock] if reg.lock is not None else None
        log = _log.bind(job_id=job_id, kind=kind)
        if lock is not None:
            lock.acquire()
        try:
            if not self._store.claim(job_id):
                log.info("job.skipped", reason="not queued (cancelled)")
                return
            log.info("job.started")
            try:
                result = reg.handler(params, JobContext(job_id=job_id, _store=self._store))
            except AppError as exc:
                log.warning("job.failed", error=str(exc), error_type=type(exc).__name__)
                self._store.finish(job_id, "failed", error=str(exc))
                return
            except Exception as exc:
                log.error("job.crashed", error=str(exc), error_type=type(exc).__name__)
                self._store.finish(job_id, "failed", error=f"{type(exc).__name__}: {exc}")
                return
            try:
                self._store.finish(job_id, "succeeded", result=result)
            except Exception as exc:  # e.g. unserializable result
                log.error("job.result_unstorable", error=str(exc))
                self._store.finish(
                    job_id, "failed", error=f"could not store result: {type(exc).__name__}"
                )
                return
            log.info("job.succeeded")
        finally:
            if lock is not None:
                lock.release()


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
    )


def _parse_ts(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")
