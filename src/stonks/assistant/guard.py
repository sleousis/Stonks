"""The assistant's safety envelope around a person (roadmap 20.4).

Deterministic checks that decide what the assistant may do, whatever the
model says:

- **Frozen.** A burst of writes (more than ``max_writes_per_minute`` or
  ``max_writes_per_hour``) freezes the person's assistant for
  ``freeze_minutes``: it runs research only and writes nothing. The person
  may clear it in the web app (a fresh second factor).
- **Kill switch.** While a kill switch covers the person (global or theirs),
  the assistant is research only too.
- **Writes are counted** from ``assistant_pending_actions``: every write the
  assistant ran or proposed is a row there.

Any error while checking counts as frozen: fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from stonks.assistant.settings import AssistantEnvelope
from stonks.logging import get_logger
from stonks.production.halts import active_halts
from stonks.store.state import SqliteState

_log = get_logger("stonks.assistant.guard")


@dataclass(frozen=True)
class Gate:
    """What the assistant may do for one person right now."""

    #: No write tool at all (frozen, kill switch, research-only setting).
    research_only: bool
    #: Order drafts may be offered (the envelope allows them and not frozen).
    order_tools: bool
    frozen_until: str | None = None
    reason: str | None = None


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def frozen_until(state: SqliteState, user_id: str, now: datetime) -> tuple[str, str] | None:
    """``(until, reason)`` while the person's assistant is frozen."""
    rows = state.sql(
        "SELECT frozen_until, reason FROM assistant_freezes WHERE user_id = ? AND frozen_until > ?",
        [user_id, _iso(now)],
    )
    return (rows[0]["frozen_until"], rows[0]["reason"]) if rows else None


def freeze(state: SqliteState, user_id: str, minutes: float, reason: str, now: datetime) -> str:
    until = _iso(now + timedelta(minutes=minutes))
    state.execute(
        "INSERT INTO assistant_freezes (user_id, frozen_until, reason, created_at)"
        " VALUES (?, ?, ?, ?) ON CONFLICT (user_id) DO UPDATE SET"
        " frozen_until = excluded.frozen_until, reason = excluded.reason,"
        " created_at = excluded.created_at",
        [user_id, until, reason, _iso(now)],
    )
    _log.warning("assistant.frozen", user_id=user_id, until=until, reason=reason)
    return until


def clear_freeze(state: SqliteState, user_id: str) -> bool:
    cur = state.execute("DELETE FROM assistant_freezes WHERE user_id = ?", [user_id])
    return bool(cur.rowcount)


def writes_since(state: SqliteState, user_id: str, since: datetime) -> int:
    rows = state.sql(
        "SELECT COUNT(*) AS n FROM assistant_pending_actions a"
        " JOIN assistant_conversations c ON c.id = a.conversation_id"
        " WHERE c.owner_id = ? AND a.created_at >= ?",
        [user_id, _iso(since)],
    )
    return int(rows[0]["n"])


def over_rate(
    state: SqliteState, user_id: str, envelope: AssistantEnvelope, now: datetime
) -> str | None:
    """Why one more write would be a burst, or ``None``."""
    minute = writes_since(state, user_id, now - timedelta(minutes=1))
    if minute >= envelope.max_writes_per_minute:
        return f"more than {envelope.max_writes_per_minute} writes in a minute"
    hour = writes_since(state, user_id, now - timedelta(hours=1))
    if hour >= envelope.max_writes_per_hour:
        return f"more than {envelope.max_writes_per_hour} writes in an hour"
    return None


def kill_in_force(state: SqliteState, user_id: str, now: datetime) -> bool:
    """A kill switch covers the person: global, or on all their books."""
    return any(
        h.kind == "kill" and h.scope in ("global", "user")
        for h in active_halts(state, now.date(), user_id=user_id)
    )


def gate_for(
    state: SqliteState,
    user_id: str,
    envelope: AssistantEnvelope,
    *,
    research_only: bool = False,
    now: datetime | None = None,
) -> Gate:
    """The gate for one turn. Fails closed: an error means research only."""
    now = now or datetime.now(UTC)
    try:
        frozen = frozen_until(state, user_id, now)
        if frozen is not None:
            return Gate(True, False, frozen[0], f"frozen: {frozen[1]}")
        if kill_in_force(state, user_id, now):
            return Gate(True, False, None, "the kill switch is on")
    except Exception as exc:
        _log.error("assistant.gate_failed", user_id=user_id, error=str(exc))
        return Gate(True, False, None, "the safety check failed")
    if research_only:
        return Gate(True, False, None, "research only")
    return Gate(False, envelope.order_tools, None, None)
