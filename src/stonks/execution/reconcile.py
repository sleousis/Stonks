"""Sync broker order statuses and fills into the ``orders`` / ``fills`` tables.

Two paths, picked by broker capability:

- **Order-state path** (brokers implementing ``OrderStateSource``, e.g.
  Alpaca): every non-terminal row in ``orders`` is looked up by client_id
  (``reconcile_order``, also used by the tick right after each submit).
  Rows are written ``pending`` before submission, so a row the broker never
  saw (no broker id, no fills) is marked ``rejected`` with a reason.
  The broker's *cumulative* filled quantity is compared with what ``fills``
  already holds for that order, and only the difference is inserted. That
  makes the sync idempotent by construction and works from a fresh,
  stateless process (orders placed by an earlier tick).
- **Fill-list path** (everything else, e.g. ``SimulatedBroker``): the fills
  returned by ``broker.reconcile()`` are inserted unless an identical fill
  (same order, quantity, price, timestamp) is already recorded. Such brokers
  report completed fills only, so an order with a fill becomes ``filled``.

Both paths only touch the orders of one portfolio (``portfolio_id``,
default the default portfolio): a broker account backs exactly one
portfolio, so another portfolio's pending order must never be looked up
there (and rejected as "never received") nor receive its fills.

Per-order broker failures are soft: logged, reported in the summary, and
the order is left untouched for the next run.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from stonks.accounts.models import DEFAULT_PORTFOLIO_ID
from stonks.core.protocols import Broker
from stonks.core.types import Fill
from stonks.execution.brokers.base import OrderStateSource, delta_fill
from stonks.logging import get_logger
from stonks.production.ledger import ledger_filter
from stonks.store.state import SqliteState

_log = get_logger("stonks.execution.reconcile")

NON_TERMINAL_STATUSES = ("pending", "partially_filled")
NOT_FOUND_REASON = "not found at the broker: the submission never arrived"


@dataclass(frozen=True)
class ReconcileSummary:
    orders_checked: int = 0
    orders_updated: int = 0
    fills_inserted: int = 0
    unknown_orders: tuple[str, ...] = ()
    failed_orders: tuple[str, ...] = ()
    orphan_fills: tuple[str, ...] = ()


def reconcile_orders(
    broker: Broker,
    state: SqliteState,
    *,
    now: datetime | None = None,
    portfolio_id: str = DEFAULT_PORTFOLIO_ID,
) -> ReconcileSummary:
    """Sync the orders of ``portfolio_id`` (the portfolio ``broker`` trades)."""
    now = now or datetime.now(UTC)
    if isinstance(broker, OrderStateSource):
        summary = _reconcile_by_order_state(broker, state, now, portfolio_id)
    else:
        summary = _reconcile_by_fill_list(broker, state, now, portfolio_id)
    _log.info(
        "reconcile.done",
        orders_checked=summary.orders_checked,
        orders_updated=summary.orders_updated,
        fills_inserted=summary.fills_inserted,
        unknown=len(summary.unknown_orders),
        failed=len(summary.failed_orders),
        orphans=len(summary.orphan_fills),
    )
    return summary


# ---- order-state path --------------------------------------------------------


def _reconcile_by_order_state(
    broker: OrderStateSource, state: SqliteState, now: datetime, portfolio_id: str
) -> ReconcileSummary:
    placeholders = ",".join("?" for _ in NON_TERMINAL_STATUSES)
    where, params = ledger_filter(state, "orders", portfolio_id)
    rows = state.sql(
        f"SELECT client_id FROM orders WHERE status IN ({placeholders}) AND {where}"
        " ORDER BY created_at",
        [*NON_TERMINAL_STATUSES, *params],
    )
    updated = inserted = 0
    unknown: list[str] = []
    failed: list[str] = []
    for row in rows:
        client_id = row["client_id"]
        try:
            outcome = reconcile_order(broker, state, client_id, now=now)
        except Exception as exc:
            _log.warning("reconcile.order_failed", client_id=client_id, error=str(exc))
            failed.append(client_id)
            continue
        if outcome.unknown:
            unknown.append(client_id)
        updated += outcome.updated
        inserted += outcome.fill_inserted
    return ReconcileSummary(
        orders_checked=len(rows),
        orders_updated=updated,
        fills_inserted=inserted,
        unknown_orders=tuple(unknown),
        failed_orders=tuple(failed),
    )


@dataclass(frozen=True)
class OrderSync:
    """What ``reconcile_order`` did to one ledger row."""

    #: The broker has never seen the client_id.
    unknown: bool = False
    #: The row's status / broker_order_id / reason changed.
    updated: bool = False
    fill_inserted: bool = False


def reconcile_order(
    broker: OrderStateSource,
    state: SqliteState,
    client_id: str,
    *,
    now: datetime | None = None,
    reject_unknown: bool = True,
) -> OrderSync:
    """Sync one ``orders`` row with the broker's state for its client_id:
    book the not-yet-recorded fill delta and update status and
    broker_order_id, in one transaction. Broker errors propagate.

    Rows are written ``pending`` *before* submission, so a row the broker
    has never seen, with no broker id and no fills, was never received (the
    process died between the write and the submit): it becomes ``rejected``
    with a reason, which also lets a same-day rerun resubmit it. A row with
    a broker id or fills was received, so "not found" there is a broker
    anomaly and the row is left alone. ``reject_unknown=False`` never
    rejects (right after a submit the broker may not list the order yet)."""
    now = now or datetime.now(UTC)
    broker_state = broker.get_order_state(client_id)
    if broker_state is None:
        _log.warning("reconcile.order_unknown_to_broker", client_id=client_id)
        rejected = reject_unknown and _reject_never_received(state, client_id, now)
        return OrderSync(unknown=True, updated=rejected)

    with state.transaction():
        booked_qty, booked_notional = _booked(state, client_id)
        fill = delta_fill(
            broker_state,
            recorded_quantity=booked_qty,
            recorded_notional=booked_notional,
            filled_at=broker_state.updated_at or now,
        )
        if fill is not None:
            _insert_fill(state, fill)
        cur = state.execute(
            """
            UPDATE orders
               SET status = ?, broker_order_id = ?, updated_at = ?
             WHERE client_id = ?
               AND (status IS NOT ? OR broker_order_id IS NOT ?)
            """,
            [
                broker_state.status,
                broker_state.broker_order_id,
                _iso(now),
                client_id,
                broker_state.status,
                broker_state.broker_order_id,
            ],
        )
    return OrderSync(updated=cur.rowcount > 0, fill_inserted=fill is not None)


def _reject_never_received(state: SqliteState, client_id: str, now: datetime) -> bool:
    cur = state.execute(
        """
        UPDATE orders
           SET status = 'rejected', status_reason = ?, updated_at = ?
         WHERE client_id = ?
           AND status = 'pending'
           AND broker_order_id IS NULL
           AND NOT EXISTS (SELECT 1 FROM fills WHERE order_client_id = orders.client_id)
        """,
        [NOT_FOUND_REASON, _iso(now), client_id],
    )
    if cur.rowcount:
        _log.warning("reconcile.order_never_received", client_id=client_id)
    return cur.rowcount > 0


def _booked(state: SqliteState, client_id: str) -> tuple[float, float]:
    row = state.sql(
        "SELECT COALESCE(SUM(quantity), 0) AS qty, COALESCE(SUM(quantity * price), 0) AS notional"
        " FROM fills WHERE order_client_id = ?",
        [client_id],
    )[0]
    return float(row["qty"]), float(row["notional"])


# ---- fill-list path -----------------------------------------------------------


def _reconcile_by_fill_list(
    broker: Broker, state: SqliteState, now: datetime, portfolio_id: str
) -> ReconcileSummary:
    fills = broker.reconcile()
    where, params = ledger_filter(state, "orders", portfolio_id)
    updated = inserted = 0
    orphans: list[str] = []
    checked: set[str] = set()
    for fill in fills:
        client_id = fill.order_client_id
        checked.add(client_id)
        if not state.sql(
            f"SELECT 1 FROM orders WHERE client_id = ? AND {where}", [client_id, *params]
        ):
            _log.warning("reconcile.fill_without_order", client_id=client_id)
            orphans.append(client_id)
            continue
        with state.transaction():
            if not _fill_exists(state, fill):
                _insert_fill(state, fill)
                inserted += 1
            cur = state.execute(
                "UPDATE orders SET status = 'filled', updated_at = ?"
                " WHERE client_id = ? AND status IS NOT 'filled'",
                [_iso(now), client_id],
            )
            updated += cur.rowcount
    return ReconcileSummary(
        orders_checked=len(checked),
        orders_updated=updated,
        fills_inserted=inserted,
        orphan_fills=tuple(orphans),
    )


def _fill_exists(state: SqliteState, fill: Fill) -> bool:
    rows = state.sql(
        "SELECT 1 FROM fills WHERE order_client_id = ? AND quantity = ? AND price = ?"
        " AND filled_at = ?",
        [fill.order_client_id, fill.quantity, fill.price, _iso(fill.filled_at)],
    )
    return bool(rows)


# ---- shared -------------------------------------------------------------------


def _insert_fill(state: SqliteState, fill: Fill) -> None:
    # Same timestamp format as production.tick._record_fill so rows written
    # by either path compare equal.
    state.execute(
        "INSERT INTO fills (order_client_id, ticker, quantity, price, fee, filled_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            fill.order_client_id,
            fill.ticker,
            fill.quantity,
            fill.price,
            fill.fee,
            _iso(fill.filled_at),
        ],
    )


def _iso(ts: datetime) -> str:
    return ts.isoformat(timespec="seconds")
