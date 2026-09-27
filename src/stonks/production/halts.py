"""The halt store (BL-28 W3.2, roadmap 12.6): ``risk_halts`` rows that stop
new orders at global, user or portfolio scope.

- :func:`trip_halt` opens a halt (idempotent while one of the same kind and
  target is open; an expired one is closed first);
- :func:`escalate_halt` turns an open ``buys`` halt into an ``all`` one;
- :func:`active_halts` lists the halts in force for a portfolio on a day
  (global, its owner's, its own);
- :func:`clear_halt` is the logged reset: it needs an actor and a reason,
  closes the row once and writes a ``status_changes`` row of kind
  ``risk_reset``;
- :func:`sync_operational_halt` turns a health report into the global
  ``operational`` halt (opened on stale data or a stuck run, cleared by the
  next healthy report);
- :func:`halt_health_check` is the health check for open halts;
- :func:`run_health` is ``check_health`` plus both of the above, what the
  trusted health entry points (scheduled job, CLI, admin route) run;
- :func:`read_health` is the read-only report (``GET /api/health/report``);
- :func:`notify_trip` sends the ``risk`` notification of a trip.

Who may trip or clear what (owners, admins, typed confirmation for the
kill switch) is the app layer's job (``stonks.app.halts``); this module
only keeps the rows consistent.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Literal, get_args

from stonks.logging import get_logger
from stonks.production.health import HealthCheck, HealthReport, check_health
from stonks.store.state import SqliteState

if TYPE_CHECKING:
    from stonks.config import HealthConfig
    from stonks.store.lake import DuckDBLake

__all__ = [
    "HALT_KINDS",
    "OPERATIONAL_CHECKS",
    "Halt",
    "HaltError",
    "HaltKind",
    "HaltMode",
    "HaltScope",
    "active_halts",
    "clear_halt",
    "escalate_halt",
    "get_halt",
    "halt_health_check",
    "halts_enabled",
    "list_halts",
    "notify_trip",
    "read_health",
    "run_health",
    "sync_operational_halt",
    "trip_halt",
]

_log = get_logger("stonks.production.halts")

HaltKind = Literal["month_loss", "week_loss", "drawdown", "operational", "kill"]
HaltScope = Literal["global", "user", "portfolio"]
HaltMode = Literal["buys", "all"]
HALT_KINDS: tuple[str, ...] = get_args(HaltKind)

TABLE = "risk_halts"
#: Health checks whose failure opens the operational halt (by name prefix).
OPERATIONAL_CHECKS = ("freshness", "stuck_ticks", "stuck_ingest_runs")
HEALTH_ACTOR = "service:health"


class HaltError(ValueError):
    """A halt request that breaks a rule (missing reason, cleared twice, ...)."""


@dataclass(frozen=True)
class Halt:
    id: int
    kind: HaltKind
    scope: HaltScope
    user_id: str | None
    portfolio_id: str | None
    halt: HaltMode
    reason: str
    tripped_by: str
    tripped_at: str
    expires_on: date | None
    cleared_at: str | None = None
    cleared_by: str | None = None
    clear_reason: str | None = None

    @property
    def cleared(self) -> bool:
        return self.cleared_at is not None

    def active_on(self, day: date) -> bool:
        return not self.cleared and (self.expires_on is None or day < self.expires_on)

    @property
    def target(self) -> str:
        if self.scope == "global":
            return "global"
        if self.scope == "user":
            return f"user {self.user_id}"
        return f"portfolio {self.portfolio_id}"

    @classmethod
    def from_row(cls, row: Any) -> Halt:
        return cls(
            id=int(row["id"]),
            kind=row["kind"],
            scope=row["scope"],
            user_id=row["user_id"],
            portfolio_id=row["portfolio_id"],
            halt=row["halt"],
            reason=row["reason"],
            tripped_by=row["tripped_by"],
            tripped_at=row["tripped_at"],
            expires_on=date.fromisoformat(row["expires_on"]) if row["expires_on"] else None,
            cleared_at=row["cleared_at"],
            cleared_by=row["cleared_by"],
            clear_reason=row["clear_reason"],
        )


def halts_enabled(state: SqliteState) -> bool:
    """Whether ``risk_halts`` exists (migration 016 applied)."""
    rows = state.sql("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", [TABLE])
    return bool(rows)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _today() -> date:
    return datetime.now(UTC).date()


def _required(value: str | None, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HaltError(f"a {what} is required")
    return value.strip()


def _target(
    scope: HaltScope, user_id: str | None, portfolio_id: str | None
) -> tuple[str | None, str | None]:
    if scope == "global":
        return None, None
    if scope == "user":
        if not user_id:
            raise HaltError("a user halt needs a user_id")
        return user_id, None
    if scope == "portfolio":
        if not portfolio_id:
            raise HaltError("a portfolio halt needs a portfolio_id")
        return None, portfolio_id
    raise HaltError(f"unknown halt scope {scope!r}")


def get_halt(state: SqliteState, halt_id: int) -> Halt:
    rows = state.sql(f"SELECT * FROM {TABLE} WHERE id = ?", [halt_id])
    if not rows:
        raise HaltError(f"no halt with id {halt_id}")
    return Halt.from_row(rows[0])


def _open(
    state: SqliteState,
    kind: str,
    scope: str,
    user_id: str | None,
    portfolio_id: str | None,
) -> Halt | None:
    rows = state.sql(
        f"SELECT * FROM {TABLE} WHERE kind = ? AND scope = ? AND cleared_at IS NULL"
        " AND COALESCE(user_id, '') = ? AND COALESCE(portfolio_id, '') = ?",
        [kind, scope, user_id or "", portfolio_id or ""],
    )
    return Halt.from_row(rows[0]) if rows else None


def _close(state: SqliteState, halt_id: int, actor: str, reason: str) -> None:
    state.execute(
        f"UPDATE {TABLE} SET cleared_at = ?, cleared_by = ?, clear_reason = ?"
        " WHERE id = ? AND cleared_at IS NULL",
        [_now(), actor, reason, halt_id],
    )


def trip_halt(
    state: SqliteState,
    kind: HaltKind,
    *,
    reason: str,
    actor: str,
    scope: HaltScope = "portfolio",
    portfolio_id: str | None = None,
    user_id: str | None = None,
    halt: HaltMode = "buys",
    expires_on: date | None = None,
    on: date | None = None,
) -> tuple[Halt, bool]:
    """Open a halt; ``(halt, created)``. While a halt of the same kind and
    target is in force on ``on`` that one is returned unchanged. One that
    has expired is closed (``cleared_by = 'system'``) and a new one opened."""
    if kind not in HALT_KINDS:
        raise HaltError(f"unknown halt kind {kind!r}")
    if halt not in ("buys", "all"):
        raise HaltError(f"halt must be 'buys' or 'all', got {halt!r}")
    reason_ = _required(reason, "reason")
    actor_ = _required(actor, "actor")
    user_id, portfolio_id = _target(scope, user_id, portfolio_id)
    day = on or _today()
    with state.transaction():
        existing = _open(state, kind, scope, user_id, portfolio_id)
        if existing is not None:
            if existing.active_on(day):
                return existing, False
            _close(state, existing.id, "system", "expired")
        cur = state.execute(
            f"INSERT INTO {TABLE} (kind, scope, user_id, portfolio_id, halt, reason,"
            " tripped_by, tripped_at, expires_on) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                kind,
                scope,
                user_id,
                portfolio_id,
                halt,
                reason_,
                actor_,
                _now(),
                expires_on.isoformat() if expires_on else None,
            ],
        )
        created = get_halt(state, int(cur.lastrowid or 0))
    _log.warning(
        "halt.tripped",
        halt_id=created.id,
        kind=kind,
        scope=scope,
        user_id=user_id,
        portfolio_id=portfolio_id,
        halt=halt,
        actor=actor_,
        reason=reason_,
    )
    return created, True


def escalate_halt(state: SqliteState, halt_id: int, *, actor: str, reason: str) -> Halt:
    """Make an open ``buys`` halt stop every order (TO-07). Rows are
    append-only, so the ``buys`` row is closed (``clear_reason`` says it
    was escalated) and an ``all`` row of the same kind and target opens in
    the same transaction: no moment without a halt. An ``all`` halt is
    returned unchanged (never downgraded)."""
    actor_ = _required(actor, "actor")
    reason_ = _required(reason, "reason")
    with state.transaction():
        old = get_halt(state, halt_id)
        if old.cleared:
            raise HaltError(f"halt {halt_id} was already cleared")
        if old.halt == "all":
            return old
        _close(state, old.id, actor_, f"escalated to all: {reason_}")
        cur = state.execute(
            f"INSERT INTO {TABLE} (kind, scope, user_id, portfolio_id, halt, reason,"
            " tripped_by, tripped_at, expires_on) VALUES (?, ?, ?, ?, 'all', ?, ?, ?, ?)",
            [
                old.kind,
                old.scope,
                old.user_id,
                old.portfolio_id,
                reason_,
                actor_,
                _now(),
                old.expires_on.isoformat() if old.expires_on else None,
            ],
        )
        new = get_halt(state, int(cur.lastrowid or 0))
    _log.warning("halt.escalated", halt_id=new.id, replaced=old.id, kind=old.kind, actor=actor_)
    return new


def active_halts(
    state: SqliteState,
    on: date,
    *,
    portfolio_id: str | None = None,
    user_id: str | None = None,
) -> list[Halt]:
    """Halts in force on ``on`` for a portfolio (global, its owner
    ``user_id``'s and its own), oldest first. Without the table: none."""
    if not halts_enabled(state):
        return []
    rows = state.sql(
        f"SELECT * FROM {TABLE} WHERE cleared_at IS NULL AND ("
        " scope = 'global'"
        " OR (scope = 'user' AND user_id = ?)"
        " OR (scope = 'portfolio' AND portfolio_id = ?)"
        ") ORDER BY id",
        [user_id, portfolio_id],
    )
    return [h for h in map(Halt.from_row, rows) if h.active_on(on)]


def list_halts(
    state: SqliteState, *, include_cleared: bool = False, on: date | None = None
) -> list[Halt]:
    """Every halt, newest first; by default only those in force on ``on``."""
    rows = state.sql(f"SELECT * FROM {TABLE} ORDER BY id DESC")
    halts = [Halt.from_row(r) for r in rows]
    if include_cleared:
        return halts
    day = on or _today()
    return [h for h in halts if h.active_on(day)]


def clear_halt(state: SqliteState, halt_id: int, *, actor: str, reason: str) -> Halt:
    """The logged reset: close an open halt and write a ``risk_reset``
    row to ``status_changes`` in the same transaction."""
    actor_ = _required(actor, "actor")
    reason_ = _required(reason, "reason")
    with state.transaction():
        halt = get_halt(state, halt_id)
        if halt.cleared:
            raise HaltError(f"halt {halt_id} was already cleared")
        _close(state, halt_id, actor_, reason_)
        state.execute(
            "INSERT INTO status_changes (strategy_id, kind, actor, reason, created_at)"
            " VALUES (NULL, 'risk_reset', ?, ?, ?)",
            [actor_, f"cleared {halt.kind} halt #{halt.id} ({halt.target}): {reason_}", _now()],
        )
        cleared = get_halt(state, halt_id)
    _log.warning("halt.cleared", halt_id=halt_id, kind=halt.kind, actor=actor_, reason=reason_)
    return cleared


def sync_operational_halt(
    state: SqliteState, report: HealthReport, *, actor: str = HEALTH_ACTOR
) -> Halt | None:
    """Open the global ``operational`` halt when ``report`` finds stale data
    or a stuck run; clear it when the report no longer does. Returns the
    open halt, or ``None`` when trading may go on."""
    failing = [c for c in report.failures if c.name.split(":", 1)[0] in OPERATIONAL_CHECKS]
    on = report.checked_at.date()
    if failing:
        names = ", ".join(sorted(c.name for c in failing)[:10])
        halt, _ = trip_halt(
            state,
            "operational",
            reason=f"health: {names}",
            actor=actor,
            scope="global",
            on=on,
        )
        return halt
    existing = _open(state, "operational", "global", None, None)
    if existing is not None:
        clear_halt(state, existing.id, actor=actor, reason="health checks pass again")
    return None


def halt_health_check(state: SqliteState, on: date | None = None) -> HealthCheck:
    """``risk_halts``: fails while any halt is in force, naming each."""
    if not halts_enabled(state):
        return HealthCheck(name="risk_halts", ok=True, detail="no risk_halts table")
    halts = list_halts(state, on=on)
    if not halts:
        return HealthCheck(name="risk_halts", ok=True, detail="no halt in force")
    detail = "; ".join(f"#{h.id} {h.kind} ({h.target}, {h.halt})" for h in halts)
    return HealthCheck(name="risk_halts", ok=False, detail=detail)


def run_health(
    state: SqliteState,
    lake: DuckDBLake,
    universe: Sequence[str],
    config: HealthConfig,
    now: datetime | None = None,
    *,
    actor: str = HEALTH_ACTOR,
) -> HealthReport:
    """:func:`check_health`, then :func:`sync_operational_halt` on its
    report (as ``actor``), with :func:`halt_health_check` appended. Only
    trusted entry points run it: the scheduled health job, ``stonks
    health`` and the admin route. Reads use :func:`read_health`. Without
    the halt table this is ``check_health`` alone."""
    report = check_health(state, lake, universe, config, now=now)
    if not halts_enabled(state):
        return report
    sync_operational_halt(state, report, actor=actor)
    halt_check = halt_health_check(state, report.checked_at.date())
    return HealthReport(checks=[*report.checks, halt_check], checked_at=report.checked_at)


def read_health(
    state: SqliteState,
    lake: DuckDBLake,
    universe: Sequence[str],
    config: HealthConfig,
    now: datetime | None = None,
) -> HealthReport:
    """:func:`check_health` plus :func:`halt_health_check`, read-only: it
    never opens or clears a halt, whatever tickers it is asked about."""
    report = check_health(state, lake, universe, config, now=now)
    if not halts_enabled(state):
        return report
    halt_check = halt_health_check(state, report.checked_at.date())
    return HealthReport(checks=[*report.checks, halt_check], checked_at=report.checked_at)


Publish = Callable[[Any], Any]


def notify_trip(state: SqliteState, halt: Halt, publish: Publish | None = None) -> None:
    """Send the ``risk`` notification (high urgency) for a trip: to the
    portfolio's owner, the user, or the admins for a global halt. A failed
    send is logged, never raised: the halt is already in force."""
    from stonks.notify.events import Audience, Event
    from stonks.notify.router import configured_router

    if halt.scope == "portfolio":
        audience = Audience.owner_of(halt.portfolio_id or "")
    elif halt.scope == "user":
        audience = Audience.users(halt.user_id or "")
    else:
        audience = Audience.admins()
    event = Event(
        category="risk",
        level="error",
        title=f"Trading halted: {halt.kind.replace('_', ' ')}",
        body=f"{'New orders' if halt.halt == 'all' else 'New buys'} are blocked for "
        f"{halt.target}: {halt.reason}",
        audience=audience,
        dedupe_key=f"halt:{halt.id}",
        deep_link="/health",
        portfolio_id=halt.portfolio_id,
    )
    try:
        (publish or configured_router(state).publish)(event)
    except Exception as exc:
        _log.error("halt.notify_failed", halt_id=halt.id, error=str(exc))
