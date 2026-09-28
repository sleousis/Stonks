"""Sending live option combos from their tickets (roadmap 17.8).

A multi-leg combo is written as one ticket per leg (``options/live/orders.py``).
At submit time :func:`split_combos` pulls those tickets out of the
sendable list and groups them by combo. A combo goes out only when every
leg's ticket is here and sendable: one missing, held or rejected leg holds
them all, so the broker never receives half a spread.

:func:`send_combo` then sends the combo as one order through the broker's
``place_combo``, with the same steps as a single ticket: every leg's order
row is committed ``pending`` first, a rejection fails every leg, and a
submit with no answer leaves every leg ``unknown`` until reconciliation
settles it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from stonks.core.clock import Clock
from stonks.core.protocols import Broker
from stonks.execution.brokers.base import OrderRejectedError, OrderStateSource
from stonks.execution.order_state import current_state, mark_unknown, write_state
from stonks.execution.reconcile import reconcile_order
from stonks.logging import get_logger
from stonks.options.live.orders import combo_from_legs, combo_size
from stonks.production.tickets import Ticket, set_ticket_status
from stonks.store.state import SqliteState

_log = get_logger("stonks.production.submit_combos")


def _combo_id(ticket: Ticket) -> str | None:
    ctx = ticket.order.decision_context or {}
    value = ctx.get("combo_id")
    return str(value) if value and combo_size(ticket.order) > 1 else None


def split_combos(
    tickets: Sequence[Ticket],
) -> tuple[list[Ticket], list[list[Ticket]], int]:
    """``(single tickets, complete combos, held legs)``. A combo with a leg
    missing from ``tickets`` is held whole."""
    singles: list[Ticket] = []
    groups: dict[str, list[Ticket]] = {}
    for t in tickets:
        cid = _combo_id(t)
        if cid is None:
            singles.append(t)
        else:
            groups.setdefault(cid, []).append(t)
    complete: list[list[Ticket]] = []
    held = 0
    for cid, legs in groups.items():
        if len(legs) == combo_size(legs[0].order):
            complete.append(sorted(legs, key=lambda t: t.client_id))
        else:
            _log.warning("submit.combo_incomplete", combo_id=cid, legs=len(legs))
            held += len(legs)
    return singles, complete, held


def send_combo(
    state: SqliteState, broker: Broker, legs: Sequence[Ticket], clock: Clock
) -> Literal["sent", "failed", "known"]:
    from stonks.options.live.broker import OptionBroker
    from stonks.production.submit import ENDED, _never_arrived
    from stonks.production.tick import _record_order

    now = clock.now
    ids = [t.client_id for t in legs]
    states = {cid: current_state(state, cid) for cid in ids}
    for cid, st in states.items():
        if st == "rejected" and _never_arrived(state, cid):
            states[cid] = None
    ended = [cid for cid, st in states.items() if st in ENDED]
    if ended:
        reason = f"combo leg {ended[0]} is already {states[ended[0]]}; it is never sent again"
        for t in legs:
            set_ticket_status(state, t.id, "failed", now=now(), reason=reason)
        return "failed"
    if any(st is not None for st in states.values()):
        for t in legs:
            set_ticket_status(state, t.id, "submitted", now=now())
        return "known"
    try:
        combo = combo_from_legs([t.order for t in legs])
    except ValueError as exc:
        for t in legs:
            set_ticket_status(state, t.id, "failed", now=now(), reason=str(exc))
        return "failed"
    if not isinstance(broker, OptionBroker):
        for t in legs:
            set_ticket_status(
                state, t.id, "failed", now=now(), reason="the broker cannot trade options"
            )
        return "failed"
    with state.transaction():
        for t in legs:
            _record_order(state, t.order, status="pending", portfolio_id=t.portfolio_id)
    try:
        broker.place_combo(combo)
    except OrderRejectedError as exc:
        _log.warning("submit.combo_rejected", combo_id=combo.client_id, error=str(exc))
        with state.transaction():
            for t in legs:
                write_state(state, t.client_id, "rejected", reason=str(exc)[:500], clock=clock)
                set_ticket_status(state, t.id, "failed", now=now(), reason=str(exc))
        return "failed"
    except Exception as exc:
        _log.warning("submit.combo_no_answer", combo_id=combo.client_id, error=str(exc))
        with state.transaction():
            for t in legs:
                mark_unknown(
                    state, t.client_id, f"submit outcome unknown: {exc}"[:500], clock=clock
                )
                set_ticket_status(state, t.id, "submitted", now=now(), reason="outcome unknown")
        return "failed"
    with state.transaction():
        for t in legs:
            write_state(state, t.client_id, "submitted", clock=clock)
            set_ticket_status(state, t.id, "submitted", now=now())
    if isinstance(broker, OrderStateSource):
        for cid in ids:
            try:
                reconcile_order(broker, state, cid, reject_unknown=False)
            except Exception as exc:
                _log.warning("submit.sync_failed", client_id=cid, error=str(exc))
    return "sent"
