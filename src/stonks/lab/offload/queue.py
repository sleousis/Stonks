"""The lab worker queue: rows of the ``jobs`` table with ``executor =
'worker'`` (state migration 025), plus the ``lab_workers`` table.

The API creates the rows (``JobStore.create(..., executor="worker")``) and
never runs them. A worker claims one with a single guarded ``UPDATE``, so
two workers can never take the same job, writes a heartbeat while it runs,
and finishes it through the normal job store, so results land in the same
``result_json`` column the API reads for every job.

A running worker job whose heartbeat is older than the lease is failed by
:meth:`LabQueue.reap_stale` (the worker died or lost its disk). Every
method opens a short-lived SQLite connection, like ``JobStore``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from stonks.store.state import SqliteState

WORKER_EXECUTOR = "worker"

Outcome = Literal["succeeded", "failed", "cancelled"]
_OUTCOME_COLUMNS: dict[str, str] = {
    "succeeded": "jobs_succeeded",
    "failed": "jobs_failed",
    "cancelled": "jobs_cancelled",
}


@dataclass(frozen=True)
class QueueStats:
    """What health checks and ``/metrics`` report about the queue."""

    queued: int
    running: int
    #: Age of the oldest queued job, ``None`` when nothing waits.
    oldest_queued_seconds: float | None
    #: Workers whose heartbeat is younger than the lease and that have not stopped.
    workers_alive: int
    #: Jobs finished by workers since each worker row was created.
    outcomes: dict[str, int] = field(default_factory=dict)
    #: Running worker jobs whose heartbeat is older than the lease.
    stale_running: int = 0


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat(timespec="microseconds")


class LabQueue:
    def __init__(
        self, state_path: str | Path, *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self._path = Path(state_path)
        self._clock = clock or (lambda: datetime.now(UTC))

    def _state(self) -> SqliteState:
        return SqliteState(self._path)

    def _now(self) -> str:
        return _iso(self._clock())

    # ---- jobs ------------------------------------------------------------------

    def claim_next(self, worker_id: str, kinds: Sequence[str]) -> str | None:
        """Move the oldest queued worker job of ``kinds`` to ``running`` for
        ``worker_id``; its id, or ``None`` when nothing waits."""
        wanted = sorted(set(kinds))
        if not wanted:
            return None
        marks = ", ".join("?" * len(wanted))
        now = self._now()
        with self._state() as s:
            rows = s.sql(
                "UPDATE jobs SET status='running', started_at=?, worker_id=?, heartbeat_at=? "
                "WHERE status='queued' AND id = ("
                "  SELECT id FROM jobs WHERE executor=? AND status='queued'"
                f"  AND kind IN ({marks}) ORDER BY created_at, rowid LIMIT 1"
                ") RETURNING id",
                [now, worker_id, now, WORKER_EXECUTOR, *wanted],
            )
        return str(rows[0]["id"]) if rows else None

    def heartbeat(self, job_id: str, worker_id: str) -> None:
        with self._state() as s:
            s.execute(
                "UPDATE jobs SET heartbeat_at=? WHERE id=? AND worker_id=? AND status='running'",
                [self._now(), job_id, worker_id],
            )

    def request_cancel(self, job_id: str) -> bool:
        """Flag a running worker job for cancellation; False when it is not
        one (queued jobs are cancelled directly through the job store)."""
        with self._state() as s:
            cur = s.execute(
                "UPDATE jobs SET cancel_requested_at=? "
                "WHERE id=? AND executor=? AND status='running'",
                [self._now(), job_id, WORKER_EXECUTOR],
            )
            return cur.rowcount == 1

    def cancel_requested(self, job_id: str) -> bool:
        with self._state() as s:
            rows = s.sql("SELECT cancel_requested_at FROM jobs WHERE id=?", [job_id])
        return bool(rows) and rows[0]["cancel_requested_at"] is not None

    def is_pending(self, job_id: str) -> bool:
        """True while ``job_id`` is a worker job that is queued or running."""
        with self._state() as s:
            rows = s.sql(
                "SELECT 1 FROM jobs WHERE id=? AND executor=? AND status IN ('queued', 'running')",
                [job_id, WORKER_EXECUTOR],
            )
        return bool(rows)

    def requeue(self, job_id: str, worker_id: str) -> bool:
        """Hand a job ``worker_id`` stopped because it was shutting down
        back to the queue, as if never claimed (BE-42). False when the job
        is not one that worker ended as cancelled."""
        with self._state() as s:
            cur = s.execute(
                "UPDATE jobs SET status='queued', started_at=NULL, finished_at=NULL, "
                "worker_id=NULL, heartbeat_at=NULL, cancel_requested_at=NULL, error=NULL, "
                "result_json=NULL, progress=0, progress_message=NULL "
                "WHERE id=? AND worker_id=? AND executor=? AND status='cancelled'",
                [job_id, worker_id, WORKER_EXECUTOR],
            )
            return cur.rowcount == 1

    def reap_stale(self, lease_seconds: float) -> list[str]:
        """Fail running worker jobs with no heartbeat for ``lease_seconds``."""
        now = self._clock()
        cutoff = _iso(now - timedelta(seconds=lease_seconds))
        with self._state() as s:
            rows = s.sql(
                "UPDATE jobs SET status='failed', finished_at=?, error=? "
                "WHERE executor=? AND status='running' AND heartbeat_at < ? RETURNING id",
                [
                    _iso(now),
                    f"worker lost: no heartbeat for {lease_seconds:g} s",
                    WORKER_EXECUTOR,
                    cutoff,
                ],
            )
        return sorted(str(r["id"]) for r in rows)

    # ---- workers ---------------------------------------------------------------

    def register_worker(self, worker_id: str, *, host: str, pid: int, cpus: int) -> None:
        """Add ``worker_id``, or restart it under the same id (BE-42): the
        row keeps its job counters and gets a new process and start."""
        now = self._now()
        with self._state() as s:
            s.execute(
                "INSERT INTO lab_workers (id, host, pid, cpus, started_at, heartbeat_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET host = excluded.host, pid = excluded.pid, "
                "cpus = excluded.cpus, started_at = excluded.started_at, "
                "heartbeat_at = excluded.heartbeat_at, stopped_at = NULL, current_job_id = NULL",
                [worker_id, host, pid, cpus, now, now],
            )

    def beat_worker(self, worker_id: str, current_job_id: str | None = None) -> None:
        with self._state() as s:
            s.execute(
                "UPDATE lab_workers SET heartbeat_at=?, current_job_id=? WHERE id=?",
                [self._now(), current_job_id, worker_id],
            )

    def worker_done(self, worker_id: str, outcome: Outcome) -> None:
        column = _OUTCOME_COLUMNS[outcome]
        with self._state() as s:
            s.execute(
                f"UPDATE lab_workers SET {column} = {column} + 1, current_job_id=NULL WHERE id=?",
                [worker_id],
            )

    def stop_worker(self, worker_id: str) -> None:
        with self._state() as s:
            s.execute(
                "UPDATE lab_workers SET stopped_at=?, current_job_id=NULL WHERE id=?",
                [self._now(), worker_id],
            )

    # ---- reads -----------------------------------------------------------------

    def stats(self, lease_seconds: float) -> QueueStats:
        with self._state() as s:
            return queue_stats(s, lease_seconds=lease_seconds, now=self._clock())


def queue_stats(state: SqliteState, *, lease_seconds: float, now: datetime) -> QueueStats:
    """:class:`QueueStats` read on an open state connection."""
    cutoff = _iso(now - timedelta(seconds=lease_seconds))
    s = state
    counts = {
        r["status"]: int(r["n"])
        for r in s.sql(
            "SELECT status, COUNT(*) AS n FROM jobs "
            "WHERE executor=? AND status IN ('queued', 'running') GROUP BY status",
            [WORKER_EXECUTOR],
        )
    }
    oldest = s.sql(
        "SELECT MIN(created_at) AS m FROM jobs WHERE executor=? AND status='queued'",
        [WORKER_EXECUTOR],
    )[0]["m"]
    stale = int(
        s.sql(
            "SELECT COUNT(*) AS n FROM jobs "
            "WHERE executor=? AND status='running' AND heartbeat_at < ?",
            [WORKER_EXECUTOR, cutoff],
        )[0]["n"]
    )
    alive = int(
        s.sql(
            "SELECT COUNT(*) AS n FROM lab_workers WHERE stopped_at IS NULL AND heartbeat_at >= ?",
            [cutoff],
        )[0]["n"]
    )
    totals = s.sql(
        "SELECT COALESCE(SUM(jobs_succeeded), 0) AS succeeded,"
        " COALESCE(SUM(jobs_failed), 0) AS failed,"
        " COALESCE(SUM(jobs_cancelled), 0) AS cancelled FROM lab_workers"
    )[0]
    oldest_age = None
    if oldest is not None:
        oldest_age = max(0.0, (now - datetime.fromisoformat(oldest)).total_seconds())
    return QueueStats(
        queued=counts.get("queued", 0),
        running=counts.get("running", 0),
        oldest_queued_seconds=oldest_age,
        workers_alive=alive,
        outcomes={k: int(totals[k]) for k in ("succeeded", "failed", "cancelled")},
        stale_running=stale,
    )
