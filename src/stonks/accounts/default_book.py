"""The default book follows every active strategy (roadmap 15.5 follow-up).

Before per-portfolio books, the tick traded ``pf_default`` over every
active strategy. With books built from subscriptions that stays true
because a strategy that turns active gets a ``pf_default`` subscription
when it has none: paper on the simulated broker, auto when the install
trades at an external broker (``[brokers].kind``), which is where the old
single book traded. SQLite migration 022 did the same once for the
strategies that were already active.

This is the one place a subscription is created in ``auto`` without the
auto gate: it keeps what the install already did, it grants nothing new.
The row is audited as ``service:system``. A subscription the owner already
has (in any mode, enabled or not) is left alone, so a strategy the owner
turned off for the default book stays off. One the system turned off (the
tick ends a retired strategy's subscription once it is flat) is turned
back on.
"""

from __future__ import annotations

import uuid

from stonks.accounts.audit import AuditLog, iso_now
from stonks.accounts.models import DEFAULT_PORTFOLIO_ID, Mode
from stonks.store.state import SqliteState

#: Written to the audit row of every default subscription.
SYSTEM_ACTOR = "service:system"


def default_mode(broker_kind: str) -> Mode:
    """The mode that matches the old single book: ``paper`` on the
    simulated broker, ``auto`` at an external broker."""
    return Mode.PAPER if broker_kind == "simulated" else Mode.AUTO


def ensure_default_subscription(state: SqliteState, strategy_id: str, mode: Mode) -> str | None:
    """Subscribe ``pf_default`` to ``strategy_id`` in ``mode`` unless it
    already has a subscription to it. Returns the new id, or ``None`` when
    nothing was written (a subscription exists, or ``pf_default`` does not)."""
    mode = Mode(mode)
    if mode is Mode.NOTIFY:
        raise ValueError("the default book trades: paper or auto, not notify")
    with state.transaction():
        owner = state.sql(
            "SELECT owner_id FROM portfolios WHERE id = ? AND status = 'active'",
            [DEFAULT_PORTFOLIO_ID],
        )
        if not owner:
            return None
        exists = state.sql(
            "SELECT id, enabled FROM subscriptions WHERE portfolio_id = ? AND strategy_id = ?",
            [DEFAULT_PORTFOLIO_ID, strategy_id],
        )
        if exists:
            row = exists[0]
            if not row["enabled"] and _system_ended(state, row["id"]):
                # the tick ended it when the strategy retired (BE-18); a
                # promotion follows the strategy again, as before
                _reenable(state, row["id"])
                return row["id"]
            return None
        sub_id = f"sub_{uuid.uuid4().hex[:12]}"
        now = iso_now()
        state.execute(
            "INSERT INTO subscriptions (id, user_id, strategy_id, portfolio_id, mode, weight,"
            " risk_overrides_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1.0, '{}', ?, ?)",
            [sub_id, owner[0]["owner_id"], strategy_id, DEFAULT_PORTFOLIO_ID, mode.value, now, now],
        )
        AuditLog(state).record(
            SYSTEM_ACTOR,
            "subscription.create",
            "subscription",
            sub_id,
            portfolio_id=DEFAULT_PORTFOLIO_ID,
            details={"strategy_id": strategy_id, "mode": mode.value, "reason": "default_book"},
        )
    return sub_id


def _system_ended(state: SqliteState, sub_id: str) -> bool:
    """The subscription's last enable or disable was the system's."""
    rows = state.sql(
        "SELECT actor FROM audit_log WHERE target_kind = 'subscription' AND target_id = ?"
        " AND action IN ('subscription.enable', 'subscription.disable')"
        " ORDER BY id DESC LIMIT 1",
        [sub_id],
    )
    return bool(rows) and rows[0]["actor"] == SYSTEM_ACTOR


def _reenable(state: SqliteState, sub_id: str) -> None:
    state.execute(
        "UPDATE subscriptions SET enabled = 1, updated_at = ? WHERE id = ?", [iso_now(), sub_id]
    )
    AuditLog(state).record(
        SYSTEM_ACTOR,
        "subscription.enable",
        "subscription",
        sub_id,
        portfolio_id=DEFAULT_PORTFOLIO_ID,
        details={"reason": "default_book"},
    )


def align_default_book(state: SqliteState, broker_kind: str) -> list[str]:
    """On an install that trades at an external broker, turn the system's
    paper ``pf_default`` subscriptions to auto (BE-10).

    Migration 022 subscribed ``pf_default`` in paper when it was a
    ``simulated`` portfolio (as migration 010 created it), even where the
    old single book traded live through ``[brokers].kind``. Without this the
    live default book would get no auto strategy and place no orders, not
    even exits. Only rows the system created for the default book
    (``reason = default_book``) whose mode no one changed since are
    touched. Each switch is audited as ``service:system``. Returns the ids
    switched (none on the simulated broker)."""
    if default_mode(broker_kind) is not Mode.AUTO:
        return []
    rows = state.sql(
        "SELECT s.id FROM subscriptions s WHERE s.portfolio_id = ? AND s.mode = 'paper'"
        " AND EXISTS (SELECT 1 FROM audit_log a WHERE a.target_kind = 'subscription'"
        " AND a.target_id = s.id AND a.action = 'subscription.create' AND a.actor = ?"
        " AND json_extract(a.details_json, '$.reason') = 'default_book')"
        " AND NOT EXISTS (SELECT 1 FROM audit_log a WHERE a.target_kind = 'subscription'"
        " AND a.target_id = s.id AND a.action = 'subscription.mode')"
        " ORDER BY s.id",
        [DEFAULT_PORTFOLIO_ID, SYSTEM_ACTOR],
    )
    ids = [r["id"] for r in rows]
    if not ids:
        return []
    now = iso_now()
    audit = AuditLog(state)
    with state.transaction():
        for sub_id in ids:
            state.execute(
                "UPDATE subscriptions SET mode = 'auto', auto_enabled_at = ?, auto_enabled_by = ?,"
                " paused_reason = NULL, updated_at = ? WHERE id = ? AND mode = 'paper'",
                [now, SYSTEM_ACTOR, now, sub_id],
            )
            audit.record(
                SYSTEM_ACTOR,
                "subscription.mode",
                "subscription",
                sub_id,
                portfolio_id=DEFAULT_PORTFOLIO_ID,
                details={
                    "from": "paper",
                    "to": "auto",
                    "reason": "default_book_live_broker",
                    "broker_kind": broker_kind,
                },
            )
    return ids
