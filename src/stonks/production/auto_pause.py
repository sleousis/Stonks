"""Auto mode pauses itself on broker errors (roadmap 15.5, S6).

When the tick cannot reach a portfolio's broker (the trading adapter fails
to open, the account can't be read, an order raises something other than a
plain rejection), every running auto subscription of that portfolio gets a
``paused_reason``. Nothing is placed for it again until its owner resumes
it with a fresh second factor (``PATCH /api/subscriptions/{id}`` back to
``auto``, which re-runs the checklist). Each pause writes an ``audit_log``
row by ``service:system`` and sends the owner a high-urgency ``risk``
notification. A plain rejection (``OrderRejectedError``) is not a broker
error and pauses nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from stonks.accounts.audit import AuditLog, iso_now
from stonks.logging import get_logger
from stonks.store.state import SqliteState

_log = get_logger("stonks.production.auto_pause")

ACTOR = "service:system"
#: ``paused_reason`` prefix of a broker-error pause.
BROKER_ERROR = "broker_error"
_REASON_MAX = 300


def broker_error_reason(error: BaseException | str) -> str:
    text = error if isinstance(error, str) else f"{type(error).__name__}: {error}"
    return f"{BROKER_ERROR}: {text}"[:_REASON_MAX]


def pause_auto(
    state: SqliteState,
    portfolio_id: str,
    subscription_ids: Sequence[str],
    reason: str,
    *,
    tick_id: str,
    as_of: date,
) -> list[str]:
    """Pause the running auto subscriptions among ``subscription_ids`` of
    ``portfolio_id``. Returns the ids paused now (already paused ones are
    left alone, so a re-run changes nothing)."""
    ids = list(subscription_ids)
    if not ids:
        return []
    marks = ", ".join("?" for _ in ids)
    now = iso_now()
    audit = AuditLog(state)
    with state.transaction():
        rows = state.sql(
            f"SELECT id FROM subscriptions WHERE id IN ({marks}) AND portfolio_id = ?"
            " AND mode = 'auto' AND paused_reason IS NULL ORDER BY id",
            [*ids, portfolio_id],
        )
        paused = [r["id"] for r in rows]
        for sub_id in paused:
            state.execute(
                "UPDATE subscriptions SET paused_reason = ?, updated_at = ? WHERE id = ?",
                [reason, now, sub_id],
            )
            audit.record(
                ACTOR,
                "subscription.auto_paused",
                "subscription",
                sub_id,
                portfolio_id=portfolio_id,
                details={"reason": reason, "tick_id": tick_id},
            )
    if paused:
        _log.warning("tick.auto_paused", portfolio_id=portfolio_id, subscriptions=paused,
                     reason=reason)  # fmt: skip
        _notify_owner(state, portfolio_id, reason, as_of)
    return paused


def _notify_owner(state: SqliteState, portfolio_id: str, reason: str, as_of: date) -> None:
    """A failed send is logged, never raised: the pause is already in force."""
    from stonks.notify.events import Audience, Event
    from stonks.notify.router import configured_router

    event = Event(
        category="risk",
        level="error",
        urgency="high",
        title="Auto trading paused",
        body=f"Stonks paused auto mode on this portfolio after a broker error: {reason}. "
        "Check the connection, then resume.",
        audience=Audience.owner_of(portfolio_id),
        dedupe_key=f"auto_paused:{portfolio_id}:{as_of.isoformat()}",
        deep_link="/subscriptions",
        portfolio_id=portfolio_id,
    )
    try:
        configured_router(state).publish(event)
    except Exception as exc:
        _log.error("tick.auto_pause_notify_failed", portfolio_id=portfolio_id, error=str(exc))
