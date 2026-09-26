"""Sync broker order statuses and fills into the ``orders`` / ``fills`` tables.

Two paths, picked by broker capability:

- **Order-state path** (brokers implementing ``OrderStateSource``, e.g.
  Alpaca): every non-terminal row in ``orders`` is looked up by client_id.
  The broker's *cumulative* filled quantity is compared with what ``fills``
  already holds for that order, and only the difference is inserted. That
  makes the sync idempotent by construction and works from a fresh,
  stateless process (orders placed by an earlier tick).
- **Fill-list path** (everything else, e.g. ``SimulatedBroker``): the fills
  returned by ``broker.reconcile()`` are inserted unless an identical fill
  (same order, quantity, price, timestamp) is already recorded. Such brokers
  report completed fills only, so an order with a fill becomes ``filled``.

Per-order broker failures are soft: logged, reported in the summary, and
the order is left untouched for the next run.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from stonks.core.protocols import Broker
from stonks.core.types import Fill
from stonks.execution.brokers.base import OrderStateSource, delta_fill
from stonks.logging import get_logger
from stonks.store.state import SqliteState

_log = get_logger("stonks.execution.reconcile")

NON_TERMINAL_STATUSES = ("pending", "partially_filled")


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
) -> ReconcileSummary:
    now = now or datetime.now(UTC)
    if isinstance(broker, OrderStateSource):
        summary = _reconcile_by_order_state(broker, state, now)
    else:
        summary = _reconcile_by_fill_list(broker, state, now)
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
    broker: OrderStateSource, state: SqliteState, now: datetime
) -> ReconcileSummary:
    placeholders = ",".join("?" for _ in NON_TERMINAL_STATUSES)
    rows = state.sql(
        f"SELECT client_id FROM orders WHERE status IN ({placeholders}) ORDER BY created_at",
        list(NON_TERMINAL_STATUSES),
    )
    updated = inserted = 0
    unknown: list[str] = []
    failed: list[str] = []
    for row in rows:
        client_id = row["client_id"]
        try:
            broker_state = broker.get_order_state(client_id)
        except Exception as exc:
            _log.warning("reconcile.order_failed", client_id=client_id, error=str(exc))
            failed.append(client_id)
            continue
        if broker_state is None:
            _log.warning("reconcile.order_unknown_to_broker", client_id=client_id)
            unknown.append(client_id)
            continue

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
                inserted += 1
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
            updated += cur.rowcount
    return ReconcileSummary(
        orders_checked=len(rows),
        orders_updated=updated,
        fills_inserted=inserted,
        unknown_orders=tuple(unknown),
        failed_orders=tuple(failed),
    )


def _booked(state: SqliteState, client_id: str) -> tuple[float, float]:
    row = state.sql(
        "SELECT COALESCE(SUM(quantity), 0) AS qty, COALESCE(SUM(quantity * price), 0) AS notional"
        " FROM fills WHERE order_client_id = ?",
        [client_id],
    )[0]
    return float(row["qty"]), float(row["notional"])


# ---- fill-list path -----------------------------------------------------------


def _reconcile_by_fill_list(broker: Broker, state: SqliteState, now: datetime) -> ReconcileSummary:
    fills = broker.reconcile()
    updated = inserted = 0
    orphans: list[str] = []
    checked: set[str] = set()
    for fill in fills:
        client_id = fill.order_client_id
        checked.add(client_id)
        if not state.sql("SELECT 1 FROM orders WHERE client_id = ?", [client_id]):
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
