"""Cancel the orders a portfolio still has working at its broker (TO-08).

The tick submits DAY market orders after the close, and crypto orders are
GTC, so an external broker keeps them queued until the next session. The
kill switch only stops future ticks. This module stops the queued ones:
every non-terminal ledger row of the portfolio (optionally only some
sides) is cancelled through the broker's :class:`OrderCanceller`
capability, then synced with :func:`reconcile_order`, so the ledger shows
what the broker made of it (``cancelled``, or ``filled`` when the fill won
the race) and a partial fill made before the cancel is booked once.

Per-order failures are soft: logged and reported, the row is left for the
next reconcile.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from stonks.core.types import OrderSide
from stonks.execution.brokers.base import GlobalCanceller, OrderCanceller, OrderStateSource
from stonks.execution.reconcile import NON_TERMINAL_STATUSES, reconcile_order
from stonks.logging import get_logger
from stonks.production.ledger import ledger_filter
from stonks.store.state import SqliteState

_log = get_logger("stonks.execution.cancel")

#: ``orders.status_reason`` of an order the kill switch cancelled.
CANCEL_REASON = "cancelled by the kill switch"


@dataclass(frozen=True)
class CancelSummary:
    #: Orders the broker cancelled.
    cancelled: tuple[str, ...] = ()
    #: Orders the broker had nothing to cancel for (already filled or gone).
    not_cancelled: tuple[str, ...] = ()
    #: Orders whose cancel or sync raised; left for the next reconcile.
    failed: tuple[str, ...] = ()
    #: The broker can't cancel or look orders up; nothing was touched.
    unsupported: bool = False


def cancel_working_orders(
    broker: object,
    state: SqliteState,
    *,
    portfolio_id: str,
    sides: Sequence[OrderSide] = ("buy", "sell"),
    openings_only: bool = False,
    global_fallback: bool = False,
    now: datetime | None = None,
) -> CancelSummary:
    """Cancel ``portfolio_id``'s working orders on ``sides`` at ``broker``.
    ``openings_only`` (a reduce-only halt, BE-12): only orders that open a
    position, by their recorded effect (a buy with none opens), so queued
    covers and sells of longs still go through. ``global_fallback``: when
    some cancels fail and the broker offers ``cancel_all``, cancel every
    order at the broker once, then sync the failed rows again (the kill
    switch while a tick owns the orders at IBKR, roadmap 19.17). The caller
    decides it is safe: every order at that broker is to go."""
    if not isinstance(broker, OrderCanceller) or not isinstance(broker, OrderStateSource):
        _log.warning("cancel.unsupported", portfolio_id=portfolio_id)
        return CancelSummary(unsupported=True)
    now = now or datetime.now(UTC)
    status_ph = ",".join("?" for _ in NON_TERMINAL_STATUSES)
    side_ph = ",".join("?" for _ in sides)
    where, params = ledger_filter(state, "orders", portfolio_id)
    opening = ""
    if openings_only:
        opening = (
            " AND (position_effect = 'open' OR (position_effect IS NULL AND side = 'buy'))"
            if _has_effect(state)
            else " AND side = 'buy'"
        )
    rows = state.sql(
        f"SELECT client_id FROM orders WHERE status IN ({status_ph}) AND side IN ({side_ph})"
        f"{opening} AND {where} ORDER BY created_at, client_id",
        [*NON_TERMINAL_STATUSES, *sides, *params],
    )
    cancelled: list[str] = []
    not_cancelled: list[str] = []
    failed: list[str] = []
    for row in rows:
        client_id = row["client_id"]
        try:
            accepted = broker.cancel_order(client_id)
            reconcile_order(broker, state, client_id, now=now, reject_unknown=False)
        except Exception as exc:
            _log.warning("cancel.order_failed", client_id=client_id, error=str(exc))
            failed.append(client_id)
            continue
        if accepted:
            state.execute(
                "UPDATE orders SET status_reason = ? WHERE client_id = ?",
                [CANCEL_REASON, client_id],
            )
            cancelled.append(client_id)
        else:
            not_cancelled.append(client_id)
    if failed and global_fallback and isinstance(broker, GlobalCanceller):
        failed, rescued = _cancel_all_then_sync(
            broker.cancel_all, broker, state, failed, portfolio_id, now
        )
        cancelled.extend(rescued)
    _log.warning(
        "cancel.done",
        portfolio_id=portfolio_id,
        cancelled=len(cancelled),
        not_cancelled=len(not_cancelled),
        failed=len(failed),
    )
    return CancelSummary(
        cancelled=tuple(cancelled), not_cancelled=tuple(not_cancelled), failed=tuple(failed)
    )


def _cancel_all_then_sync(
    cancel_all: Callable[[], int],
    broker: OrderStateSource,
    state: SqliteState,
    failed: list[str],
    portfolio_id: str,
    now: datetime,
) -> tuple[list[str], list[str]]:
    """One global cancel, then each failed row synced again. Returns the
    rows still failed and the ones now cancelled."""
    try:
        count = cancel_all()
    except Exception as exc:
        _log.warning("cancel.global_failed", portfolio_id=portfolio_id, error=str(exc))
        return failed, []
    _log.warning("cancel.global_fallback", portfolio_id=portfolio_id, open_orders=count)
    still: list[str] = []
    rescued: list[str] = []
    for client_id in failed:
        try:
            reconcile_order(broker, state, client_id, now=now, reject_unknown=False)
        except Exception as exc:
            _log.warning("cancel.order_failed", client_id=client_id, error=str(exc))
            still.append(client_id)
            continue
        rows = state.sql("SELECT status FROM orders WHERE client_id = ?", [client_id])
        if rows and rows[0]["status"] == "cancelled":
            state.execute(
                "UPDATE orders SET status_reason = ? WHERE client_id = ?",
                [CANCEL_REASON, client_id],
            )
            rescued.append(client_id)
        else:
            still.append(client_id)
    return still, rescued


def _has_effect(state: SqliteState) -> bool:
    rows = state.sql("SELECT name FROM pragma_table_info('orders')")
    return any(r["name"] == "position_effect" for r in rows)
