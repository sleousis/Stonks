"""Scheduled-run records over ``state.sqlite`` (migration 010).

Every call opens a short-lived connection (WAL, busy timeout), so the
scheduler loop, the watchdog thread and an API process can share the
store safely. The claim is a single ``INSERT OR IGNORE`` against
``UNIQUE (job_name, run_key)``: whoever inserts the row runs the fire,
everyone else skips it.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from stonks.scheduling.jobs import SCHEDULER_ACTOR, JobSpec
from stonks.scheduling.triggers import Fire
from stonks.store.state import SqliteState

RunStatus = Literal["running", "succeeded", "skipped", "failed"]
DONE_STATUSES: frozenset[str] = frozenset({"succeeded", "skipped"})

#: Run keys of runs started by hand (``run-now``) rather than by a trigger.
MANUAL_PREFIX = "manual:"
MANUAL_RUN_STALE_AFTER = timedelta(days=1)


@dataclass(frozen=True)
class RunRecord:
    id: str
    job_name: str
    action: str
    run_key: str
    scheduled_for: datetime
    as_of: str | None
    status: RunStatus
    catch_up: bool
    instance_id: str
    started_at: datetime
    finished_at: datetime | None
    detail: dict[str, Any] | None
    error: str | None


def _iso(when: datetime) -> str:
    # One fixed format so ISO strings sort chronologically in SQL.
    return when.astimezone(UTC).isoformat(timespec="microseconds")


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _record(row: Any) -> RunRecord:
    return RunRecord(
        id=row["id"],
        job_name=row["job_name"],
        action=row["action"],
        run_key=row["run_key"],
        scheduled_for=datetime.fromisoformat(row["scheduled_for"]),
        as_of=row["as_of"],
        status=row["status"],
        catch_up=bool(row["catch_up"]),
        instance_id=row["instance_id"],
        started_at=datetime.fromisoformat(row["started_at"]),
        finished_at=_parse(row["finished_at"]),
        detail=json.loads(row["detail_json"]) if row["detail_json"] else None,
        error=row["error"],
    )


class RunStore:
    def __init__(self, state_path: str | Path) -> None:
        self._path = Path(state_path)

    @contextmanager
    def _state(self) -> Iterator[SqliteState]:
        state = SqliteState(self._path)
        try:
            state.execute("PRAGMA busy_timeout = 10000")
            yield state
        finally:
            state.close()

    def migrate(self) -> None:
        with self._state() as s:
            s.migrate()

    # ---- runs --------------------------------------------------------------

    def claim(
        self, spec: JobSpec, fire: Fire, *, instance_id: str, now: datetime, catch_up: bool
    ) -> str | None:
        """Insert a ``running`` row for ``(spec.name, fire.key)``; the run
        id when this caller owns the fire, None when it already has a row."""
        run_id = f"srun_{uuid.uuid4().hex}"
        with self._state() as s:
            cur = s.execute(
                "INSERT OR IGNORE INTO scheduled_runs (id, job_name, action, run_key, "
                "scheduled_for, as_of, status, catch_up, actor, instance_id, started_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)",
                [
                    run_id,
                    spec.name,
                    spec.action,
                    fire.key,
                    _iso(fire.scheduled_for),
                    fire.as_of.isoformat(),
                    int(catch_up),
                    SCHEDULER_ACTOR,
                    instance_id,
                    _iso(now),
                ],
            )
            return run_id if cur.rowcount == 1 else None

    def finish(
        self,
        run_id: str,
        status: RunStatus,
        *,
        now: datetime,
        detail: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self._state() as s:
            s.execute(
                "UPDATE scheduled_runs SET status = ?, finished_at = ?, detail_json = ?, "
                "error = ? WHERE id = ? AND status = 'running'",
                [
                    status,
                    _iso(now),
                    json.dumps(detail, default=str, sort_keys=True) if detail else None,
                    error,
                    run_id,
                ],
            )

    def recover_interrupted(self, *, now: datetime) -> int:
        """Mark ``running`` rows ``failed``. Only safe while holding the
        single-instance lock: then no other scheduler is running. Manual
        runs belong to the CLI process that started them (which may still
        be running), so they are only recovered once a day old."""
        with self._state() as s:
            cur = s.execute(
                "UPDATE scheduled_runs SET status = 'failed', finished_at = ?, "
                "error = 'interrupted: scheduler stopped mid-run' WHERE status = 'running' "
                f"AND (run_key NOT LIKE '{MANUAL_PREFIX}%' OR started_at < ?)",
                [_iso(now), _iso(now - MANUAL_RUN_STALE_AFTER)],
            )
            return cur.rowcount

    def keys_with_runs(self, job_name: str, keys: list[str]) -> set[str]:
        if not keys:
            return set()
        with self._state() as s:
            rows = s.sql(
                f"SELECT run_key FROM scheduled_runs WHERE job_name = ? "
                f"AND run_key IN ({','.join('?' * len(keys))})",
                [job_name, *keys],
            )
        return {r["run_key"] for r in rows}

    def get(self, job_name: str, run_key: str) -> RunRecord | None:
        with self._state() as s:
            rows = s.sql(
                "SELECT * FROM scheduled_runs WHERE job_name = ? AND run_key = ?",
                [job_name, run_key],
            )
        return _record(rows[0]) if rows else None

    def last_scheduled_for(self, job_name: str) -> datetime | None:
        with self._state() as s:
            rows = s.sql(
                "SELECT MAX(scheduled_for) AS m FROM scheduled_runs WHERE job_name = ? "
                f"AND run_key NOT LIKE '{MANUAL_PREFIX}%'",
                [job_name],
            )
        return _parse(rows[0]["m"]) if rows else None

    def recent(self, *, job_name: str | None = None, limit: int = 50) -> list[RunRecord]:
        clause, params = ("WHERE job_name = ? ", [job_name]) if job_name else ("", [])
        with self._state() as s:
            rows = s.sql(
                f"SELECT * FROM scheduled_runs {clause}ORDER BY scheduled_for DESC, "
                "started_at DESC LIMIT ?",
                [*params, limit],
            )
        return [_record(r) for r in rows]

    def job_summaries(self) -> dict[str, dict[str, Any]]:
        """Per job: the latest run's status and time, the last success
        time (``succeeded`` only) and the count of running rows."""
        with self._state() as s:
            rows = s.sql(
                "SELECT job_name, "
                " MAX(CASE WHEN status = 'succeeded' THEN finished_at END) AS last_success, "
                " MAX(started_at) AS last_started, "
                " SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running "
                "FROM scheduled_runs GROUP BY job_name"
            )
            latest = s.sql(
                "SELECT r.job_name, r.status FROM scheduled_runs r "
                "JOIN (SELECT job_name, MAX(started_at) AS m FROM scheduled_runs "
                "GROUP BY job_name) l ON l.job_name = r.job_name AND l.m = r.started_at"
            )
        status = {r["job_name"]: r["status"] for r in latest}
        return {
            r["job_name"]: {
                "last_success": _parse(r["last_success"]),
                "last_started": _parse(r["last_started"]),
                "last_status": status.get(r["job_name"]),
                "running": int(r["running"] or 0),
            }
            for r in rows
        }

    # ---- instances ---------------------------------------------------------

    def register_instance(self, instance_id: str, *, host: str, pid: int, now: datetime) -> None:
        with self._state() as s:
            s.execute(
                "INSERT INTO scheduler_instances (id, host, pid, started_at, heartbeat_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [instance_id, host, pid, _iso(now), _iso(now)],
            )

    def heartbeat(self, instance_id: str, *, now: datetime) -> None:
        with self._state() as s:
            s.execute(
                "UPDATE scheduler_instances SET heartbeat_at = ? WHERE id = ?",
                [_iso(now), instance_id],
            )

    def mark_stopped(self, instance_id: str, *, now: datetime) -> None:
        with self._state() as s:
            s.execute(
                "UPDATE scheduler_instances SET stopped_at = ?, heartbeat_at = ? WHERE id = ?",
                [_iso(now), _iso(now), instance_id],
            )

    def first_started_at(self) -> datetime | None:
        with self._state() as s:
            rows = s.sql("SELECT MIN(started_at) AS m FROM scheduler_instances")
        return _parse(rows[0]["m"]) if rows else None

    def latest_instance(self) -> dict[str, Any] | None:
        with self._state() as s:
            rows = s.sql(
                "SELECT * FROM scheduler_instances ORDER BY started_at DESC, heartbeat_at DESC "
                "LIMIT 1"
            )
        if not rows:
            return None
        r = rows[0]
        return {
            "id": r["id"],
            "host": r["host"],
            "pid": r["pid"],
            "started_at": _parse(r["started_at"]),
            "heartbeat_at": _parse(r["heartbeat_at"]),
            "stopped_at": _parse(r["stopped_at"]),
        }

    # ---- deadline alerts ---------------------------------------------------

    def mark_deadline_alerted(self, job_name: str, run_key: str, *, now: datetime) -> bool:
        """True the first time for ``(job, key)``: the caller should alert."""
        with self._state() as s:
            cur = s.execute(
                "INSERT OR IGNORE INTO scheduler_deadline_alerts (job_name, run_key, alerted_at) "
                "VALUES (?, ?, ?)",
                [job_name, run_key, _iso(now)],
            )
            return cur.rowcount == 1
