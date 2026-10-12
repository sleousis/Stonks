"""Backfill progress in the state database (migration 061): each unit's
status, attempts, cursor and calls, when each phase last listed its units,
and the API calls spent per UTC day.

Only the thread that runs the backfill touches it (SQLite connections stay
on the thread that opened them); fetch threads report back to it."""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from stonks.store.state import SqliteState

#: Rows per INSERT batch when a phase lists its units.
_CHUNK = 5000
#: Longest error text kept per unit.
_ERROR_CHARS = 500

UNIT_STATUSES: tuple[str, ...] = ("pending", "retry", "done", "failed")


@dataclass(frozen=True)
class UnitSpec:
    """A unit a phase lists: its key, the exchange it belongs to (for
    ``--exchanges``) and its priority (lower runs first)."""

    key: str
    exchange: str | None = None
    priority: int = 0


@dataclass(frozen=True)
class Unit:
    phase: str
    key: str
    exchange: str | None
    priority: int
    cursor: str | None
    attempts: int


@dataclass(frozen=True)
class PhaseSummary:
    phase: str
    pending: int = 0
    retry: int = 0
    done: int = 0
    failed: int = 0
    calls: int = 0
    #: Calls spent by the done units (the cost of a whole unit).
    done_calls: int = 0
    rows: int = 0
    listed_at: datetime | None = None

    @property
    def remaining(self) -> int:
        return self.pending + self.retry

    @property
    def total(self) -> int:
        return self.pending + self.retry + self.done + self.failed


@dataclass(frozen=True)
class DayCalls:
    day: date
    calls: int
    vendor_used: int | None
    vendor_limit: int | None
    vendor_read_at: datetime | None


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def _parse(raw: str | None) -> datetime | None:
    return datetime.fromisoformat(raw) if raw else None


class BackfillState:
    def __init__(self, state: SqliteState, *, clock=None) -> None:
        self._state = state
        self._clock = clock or (lambda: datetime.now(UTC))

    def ready(self) -> bool:
        """Whether migration 061 is applied (``stonks db init``)."""
        return "backfill_units" in self._state.tables()

    # ---- listing ---------------------------------------------------------------

    def add_units(self, phase: str, units: Iterable[UnitSpec]) -> int:
        """Insert the units not stored yet (stored ones keep their progress)
        and stamp the phase as listed. Returns how many were new."""
        now = _iso(self._clock())
        before = self._count(phase)
        chunk: list[tuple[object, ...]] = []

        def flush() -> None:
            with self._state.transaction():
                self._state.con.executemany(
                    "INSERT OR IGNORE INTO backfill_units "
                    "(phase, unit_key, exchange, priority, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    chunk,
                )
            chunk.clear()

        for unit in units:
            chunk.append((phase, unit.key, unit.exchange, unit.priority, now, now))
            if len(chunk) >= _CHUNK:
                flush()
        if chunk:
            flush()
        after = self._count(phase)
        self._state.execute(
            "INSERT INTO backfill_phases (phase, listed_at, units) VALUES (?, ?, ?) "
            "ON CONFLICT (phase) DO UPDATE SET listed_at = excluded.listed_at, "
            "units = excluded.units",
            [phase, now, after],
        )
        return after - before

    def listed_at(self, phase: str) -> datetime | None:
        rows = self._state.sql("SELECT listed_at FROM backfill_phases WHERE phase = ?", [phase])
        return _parse(rows[0]["listed_at"]) if rows else None

    def _count(self, phase: str) -> int:
        rows = self._state.sql("SELECT COUNT(*) AS n FROM backfill_units WHERE phase = ?", [phase])
        return int(rows[0]["n"])

    # ---- picking ---------------------------------------------------------------

    def next_units(
        self,
        phase: str,
        limit: int,
        *,
        exchanges: Collection[str] | None = None,
        exclude: Collection[str] = (),
    ) -> list[Unit]:
        """Up to ``limit`` units to run now: pending ones, and retries whose
        wait is over, by priority then key. ``exclude`` keeps units already
        in flight out."""
        now = _iso(self._clock())
        sql = (
            "SELECT * FROM backfill_units WHERE phase = ? AND "
            "(status = 'pending' OR (status = 'retry' AND "
            "(next_attempt_at IS NULL OR next_attempt_at <= ?)))"
        )
        params: list[object] = [phase, now]
        if exchanges is not None:
            # market-wide units (calendars, macro) belong to no exchange and still run
            marks = ",".join("?" * len(exchanges))
            sql += f" AND (exchange IS NULL OR exchange IN ({marks}))"
            params.extend(exchanges)
        if exclude:
            marks = ",".join("?" * len(exclude))
            sql += f" AND unit_key NOT IN ({marks})"
            params.extend(exclude)
        sql += " ORDER BY priority, unit_key LIMIT ?"
        params.append(limit)
        return [
            Unit(
                phase=r["phase"],
                key=r["unit_key"],
                exchange=r["exchange"],
                priority=r["priority"],
                cursor=r["cursor"],
                attempts=r["attempts"],
            )
            for r in self._state.sql(sql, params)
        ]

    def next_retry_at(
        self, phases: Collection[str], exchanges: Collection[str] | None = None
    ) -> datetime | None:
        """When the earliest waiting retry of ``phases`` may run."""
        if not phases:
            return None
        marks = ",".join("?" * len(phases))
        sql = (
            f"SELECT MIN(next_attempt_at) AS at FROM backfill_units "
            f"WHERE status = 'retry' AND phase IN ({marks})"
        )
        params: list[object] = list(phases)
        if exchanges is not None:
            sql += f" AND (exchange IS NULL OR exchange IN ({','.join('?' * len(exchanges))}))"
            params.extend(exchanges)
        rows = self._state.sql(sql, params)
        return _parse(rows[0]["at"]) if rows else None

    # ---- outcomes --------------------------------------------------------------

    def mark_done(self, unit: Unit, *, calls: int, rows: int, note: str | None = None) -> None:
        self._state.execute(
            "UPDATE backfill_units SET status = 'done', calls = calls + ?, rows = rows + ?, "
            "last_error = ?, next_attempt_at = NULL, updated_at = ? "
            "WHERE phase = ? AND unit_key = ?",
            [calls, rows, note, _iso(self._clock()), unit.phase, unit.key],
        )

    def mark_progress(self, unit: Unit, *, cursor: str, calls: int, rows: int) -> None:
        """A step of a unit that runs in steps finished: keep the cursor and
        leave the unit pending for its next step."""
        self._state.execute(
            "UPDATE backfill_units SET status = 'pending', cursor = ?, attempts = 0, "
            "calls = calls + ?, rows = rows + ?, last_error = NULL, next_attempt_at = NULL, "
            "updated_at = ? WHERE phase = ? AND unit_key = ?",
            [cursor, calls, rows, _iso(self._clock()), unit.phase, unit.key],
        )

    def mark_failure(
        self,
        unit: Unit,
        *,
        error: str,
        calls: int,
        max_attempts: int,
        backoff: timedelta,
    ) -> str:
        """Count a failed attempt. The unit waits ``backoff`` doubled per
        earlier attempt, or is marked failed after ``max_attempts``.
        Returns the new status."""
        attempts = unit.attempts + 1
        now = self._clock()
        if attempts >= max_attempts:
            status, retry_at = "failed", None
        else:
            status = "retry"
            retry_at = _iso(now + backoff * (2 ** (attempts - 1)))
        self._state.execute(
            "UPDATE backfill_units SET status = ?, attempts = ?, calls = calls + ?, "
            "last_error = ?, next_attempt_at = ?, updated_at = ? "
            "WHERE phase = ? AND unit_key = ?",
            [
                status,
                attempts,
                calls,
                error[:_ERROR_CHARS],
                retry_at,
                _iso(now),
                unit.phase,
                unit.key,
            ],
        )
        return status

    def add_unit_calls(self, unit: Unit, calls: int) -> None:
        """Calls a unit spent on an attempt that was stopped by the budget."""
        if calls:
            self._state.execute(
                "UPDATE backfill_units SET calls = calls + ? WHERE phase = ? AND unit_key = ?",
                [calls, unit.phase, unit.key],
            )

    def retry_failed(self, phases: Collection[str] | None = None) -> int:
        """Give failed units a fresh set of attempts. Returns how many."""
        sql = (
            "UPDATE backfill_units SET status = 'retry', attempts = 0, next_attempt_at = NULL "
            "WHERE status = 'failed'"
        )
        params: list[object] = []
        if phases:
            sql += f" AND phase IN ({','.join('?' * len(phases))})"
            params.extend(phases)
        return self._state.execute(sql, params).rowcount

    def done_since(self, phase: str) -> dict[str, date]:
        """Each done unit of ``phase`` and the UTC day it finished."""
        rows = self._state.sql(
            "SELECT unit_key, updated_at FROM backfill_units WHERE phase = ? AND status = 'done'",
            [phase],
        )
        return {r["unit_key"]: datetime.fromisoformat(r["updated_at"]).date() for r in rows}

    # ---- calls per day ---------------------------------------------------------

    def add_calls(self, day: date, calls: int) -> None:
        if calls <= 0:
            return
        self._state.execute(
            "INSERT INTO backfill_calls (day, calls) VALUES (?, ?) "
            "ON CONFLICT (day) DO UPDATE SET calls = calls + excluded.calls",
            [day.isoformat(), calls],
        )

    def record_vendor_usage(
        self, day: date, used: int, limit: int | None, read_at: datetime
    ) -> None:
        self._state.execute(
            "INSERT INTO backfill_calls (day, calls, vendor_used, vendor_limit, vendor_read_at) "
            "VALUES (?, 0, ?, ?, ?) ON CONFLICT (day) DO UPDATE SET "
            "vendor_used = excluded.vendor_used, vendor_limit = excluded.vendor_limit, "
            "vendor_read_at = excluded.vendor_read_at",
            [day.isoformat(), used, limit, _iso(read_at)],
        )

    def calls_on(self, day: date) -> DayCalls:
        rows = self._state.sql("SELECT * FROM backfill_calls WHERE day = ?", [day.isoformat()])
        if not rows:
            return DayCalls(day, 0, None, None, None)
        r = rows[0]
        return DayCalls(
            day=day,
            calls=int(r["calls"]),
            vendor_used=r["vendor_used"],
            vendor_limit=r["vendor_limit"],
            vendor_read_at=_parse(r["vendor_read_at"]),
        )

    def recent_calls(self, days: int) -> list[DayCalls]:
        rows = self._state.sql("SELECT day FROM backfill_calls ORDER BY day DESC LIMIT ?", [days])
        return [self.calls_on(date.fromisoformat(r["day"])) for r in rows]

    # ---- summary ---------------------------------------------------------------

    def summary(self) -> dict[str, PhaseSummary]:
        out: dict[str, dict[str, object]] = {}
        for r in self._state.sql(
            "SELECT phase, status, COUNT(*) AS n, SUM(calls) AS calls, SUM(rows) AS rows "
            "FROM backfill_units GROUP BY phase, status"
        ):
            entry = out.setdefault(r["phase"], {"phase": r["phase"], "calls": 0, "rows": 0})
            entry[r["status"]] = int(r["n"])
            if r["status"] == "done":
                entry["done_calls"] = int(r["calls"] or 0)
            entry["calls"] = int(entry["calls"]) + int(r["calls"] or 0)  # type: ignore[arg-type]
            entry["rows"] = int(entry["rows"]) + int(r["rows"] or 0)  # type: ignore[arg-type]
        for r in self._state.sql("SELECT phase, listed_at FROM backfill_phases"):
            entry = out.setdefault(r["phase"], {"phase": r["phase"]})
            entry["listed_at"] = _parse(r["listed_at"])
        return {k: PhaseSummary(**v) for k, v in out.items()}  # type: ignore[arg-type]
