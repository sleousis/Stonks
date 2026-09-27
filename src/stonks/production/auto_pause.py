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

Auto also needs an ``active`` strategy (checklist item 1). When a strategy
leaves ``active`` (demoted to shadow, retired), every running auto
subscription to it pauses with ``strategy_not_active: <status>``
(:func:`pause_auto_for_strategy`, called by the registry's ``set_status``),
and the tick refuses and pauses any auto subscription whose strategy is not
active (BE-01). Paper and notify subscriptions are left alone.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime

from stonks.accounts.audit import AuditLog, iso_now
from stonks.logging import get_logger
from stonks.store.state import SqliteState

_log = get_logger("stonks.production.auto_pause")

ACTOR = "service:system"
#: ``paused_reason`` prefix of a broker-error pause.
BROKER_ERROR = "broker_error"
#: ``paused_reason`` prefix of a pause because the strategy left ``active``.
STRATEGY_NOT_ACTIVE = "strategy_not_active"
_REASON_MAX = 300


def broker_error_reason(error: BaseException | str) -> str:
    text = error if isinstance(error, str) else f"{type(error).__name__}: {error}"
    return f"{BROKER_ERROR}: {text}"[:_REASON_MAX]


def strategy_not_active_reason(status: str) -> str:
    return f"{STRATEGY_NOT_ACTIVE}: {status}"


def pause_auto(
    state: SqliteState,
    portfolio_id: str,
    subscription_ids: Sequence[str],
    reason: str,
    *,
    tick_id: str | None,
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
                details={"reason": reason, **({"tick_id": tick_id} if tick_id else {})},
            )
    if paused:
        _log.warning("tick.auto_paused", portfolio_id=portfolio_id, subscriptions=paused,
                     reason=reason)  # fmt: skip
        _notify_owner(state, portfolio_id, reason, as_of)
    return paused


def pause_auto_for_strategy(
    state: SqliteState, strategy_id: str, status: str, *, as_of: date | None = None
) -> list[str]:
    """Pause every running auto subscription to ``strategy_id``, which just
    left ``active`` for ``status``. Joins an open transaction (the status
    change's), so both commit together; the owners are told after. Returns
    the paused ids (none on a schema without subscriptions)."""
    if not state.sql("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'subscriptions'"):
        return []
    rows = state.sql(
        "SELECT id, portfolio_id FROM subscriptions WHERE strategy_id = ? AND mode = 'auto'"
        " AND paused_reason IS NULL AND portfolio_id IS NOT NULL ORDER BY portfolio_id, id",
        [strategy_id],
    )
    by_portfolio: dict[str, list[str]] = {}
    for r in rows:
        by_portfolio.setdefault(r["portfolio_id"], []).append(r["id"])
    day = as_of or datetime.now(UTC).date()
    paused: list[str] = []
    for portfolio_id, ids in by_portfolio.items():
        paused += pause_auto(
            state, portfolio_id, ids, strategy_not_active_reason(status), tick_id=None, as_of=day
        )
    return paused


def _notify_owner(state: SqliteState, portfolio_id: str, reason: str, as_of: date) -> None:
    """A failed send is logged, never raised: the pause is already in force."""
    from stonks.notify.events import Audience, Event
    from stonks.notify.router import configured_router

    if reason.startswith(STRATEGY_NOT_ACTIVE):
        body = (
            f"Stonks paused auto mode on this portfolio because its strategy is no longer "
            f"active ({reason}). Nothing is placed for it. Check your holdings at the broker."
        )
    else:
        body = (
            f"Stonks paused auto mode on this portfolio after a broker error: {reason}. "
            "Check the connection, then resume."
        )
    event = Event(
        category="risk",
        level="error",
        urgency="high",
        title="Auto trading paused",
        body=body,
        audience=Audience.owner_of(portfolio_id),
        dedupe_key=f"auto_paused:{portfolio_id}:{reason.split(':', 1)[0]}:{as_of.isoformat()}",
        deep_link="/subscriptions",
        portfolio_id=portfolio_id,
    )
    try:
        configured_router(state).publish(event)
    except Exception as exc:
        _log.error("tick.auto_pause_notify_failed", portfolio_id=portfolio_id, error=str(exc))
