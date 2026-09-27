"""Sync broker order statuses and fills into the ``orders`` / ``fills`` tables.

Three paths, picked by broker capability:

- **Execution path** (brokers implementing ``ExecutionSource``, e.g. the
  IBKR adapter, roadmap 19.1): fills are booked from the broker's
  executions, one row per execution id (``fills.broker_exec_id``, unique
  per portfolio), so a repeat is a no-op and a commission that arrives
  later updates the fill's fee. The order's status and broker id then
  come from ``get_order_state`` when the broker has it, without booking a
  second, cumulative fill. Otherwise they come from the booked quantity.

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

Every status change goes through the order state machine
(``execution.order_state``): the fine ``orders.state`` and the coarse
``status`` move together, and a change the machine forbids (a terminal
order the broker now reports as working, say) is refused and reported as a
failed order. ``unknown`` orders (a timed-out submit or cancel) are
resolved here by client id. :func:`startup_reconcile` is the check a submit
window waits for.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from stonks.accounts.models import DEFAULT_PORTFOLIO_ID
from stonks.core.clock import SYSTEM_CLOCK, Clock, FixedClock
from stonks.core.protocols import Broker
from stonks.core.types import Fill, Order
from stonks.execution.brokers.base import (
    QTY_EPSILON,
    BrokerOrderState,
    Execution,
    ExecutionSource,
    OrderState,
    OrderStateSource,
    delta_fill,
)
from stonks.execution.order_state import (
    IllegalTransitionError,
    can_transition,
    current_state,
    ledger_status,
    state_from_status,
    unknown_orders,
)
from stonks.logging import get_logger
from stonks.production.ledger import ledger_columns, ledger_filter
from stonks.store.state import SqliteState

_log = get_logger("stonks.execution.reconcile")

NON_TERMINAL_STATUSES = ("pending", "partially_filled")
NOT_FOUND_REASON = "not found at the broker: the submission never arrived"
#: How far back the execution path asks for executions: brokers keep about
#: a day (orders must settle the same session, the end-of-day check says).
EXECUTIONS_LOOKBACK = timedelta(days=1)


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
    if isinstance(broker, ExecutionSource):
        summary = _reconcile_by_executions(broker, state, now, portfolio_id)
    elif isinstance(broker, OrderStateSource):
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
    broker: OrderStateSource,
    state: SqliteState,
    now: datetime,
    portfolio_id: str,
    *,
    book_fill: bool = True,
    client_ids: Sequence[str] | None = None,
) -> ReconcileSummary:
    rows = _open_client_ids(state, portfolio_id) if client_ids is None else list(client_ids)
    updated = inserted = 0
    unknown: list[str] = []
    failed: list[str] = []
    for client_id in rows:
        try:
            outcome = reconcile_order(broker, state, client_id, now=now, book_fill=book_fill)
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


def _open_client_ids(state: SqliteState, portfolio_id: str) -> list[str]:
    """The non-terminal orders of ``portfolio_id``, oldest first."""
    placeholders = ",".join("?" for _ in NON_TERMINAL_STATUSES)
    where, params = ledger_filter(state, "orders", portfolio_id)
    rows = state.sql(
        f"SELECT client_id FROM orders WHERE status IN ({placeholders}) AND {where}"
        " ORDER BY created_at",
        [*NON_TERMINAL_STATUSES, *params],
    )
    return [r["client_id"] for r in rows]


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
    book_fill: bool = True,
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
    rejects (right after a submit the broker may not list the order yet).
    ``book_fill=False`` syncs status and broker id only: brokers that
    report executions book their fills from those (roadmap 19.1)."""
    now = now or datetime.now(UTC)
    broker_state = broker.get_order_state(client_id)
    if broker_state is None:
        _log.warning("reconcile.order_unknown_to_broker", client_id=client_id)
        rejected = reject_unknown and _reject_never_received(state, client_id, now)
        return OrderSync(unknown=True, updated=rejected)

    current = current_state(state, client_id)
    target = broker_target(broker_state, current)
    if current is not None and not can_transition(current, target):
        _log.warning(
            "reconcile.illegal_transition", client_id=client_id, current=current, broker=target
        )
        raise IllegalTransitionError(
            f"order {client_id}: the broker reports {target} but the ledger has {current}"
        )
    with state.transaction():
        booked_qty, booked_notional = _booked(state, client_id)
        fill = (
            delta_fill(
                broker_state,
                recorded_quantity=booked_qty,
                recorded_notional=booked_notional,
                filled_at=broker_state.updated_at or now,
            )
            if book_fill
            else None
        )
        if fill is not None:
            _insert_fill(state, fill)
        status = ledger_status(target)
        if _has_state(state):
            cur = state.execute(
                """
                UPDATE orders
                   SET status = ?, state = ?, broker_order_id = ?, updated_at = ?
                 WHERE client_id = ?
                   AND (status IS NOT ? OR state IS NOT ? OR broker_order_id IS NOT ?)
                """,
                [
                    status,
                    target,
                    broker_state.broker_order_id,
                    _iso(now),
                    client_id,
                    status,
                    target,
                    broker_state.broker_order_id,
                ],
            )
        else:  # pragma: no cover - a state DB before migration 028
            cur = state.execute(
                "UPDATE orders SET status = ?, broker_order_id = ?, updated_at = ?"
                " WHERE client_id = ? AND (status IS NOT ? OR broker_order_id IS NOT ?)",
                [status, broker_state.broker_order_id, _iso(now), client_id, status,
                 broker_state.broker_order_id],
            )  # fmt: skip
    return OrderSync(updated=cur.rowcount > 0, fill_inserted=fill is not None)


def broker_target(broker_state: BrokerOrderState, current: OrderState | None) -> OrderState:
    """The state the broker's answer moves an order to. A broker that only
    reports the coarse ``pending`` means the order is working: ``accepted``
    for a row that was not yet known to be at the broker, else its current
    working state stays."""
    if broker_state.state is not None:
        return broker_state.state
    coarse = state_from_status(broker_state.status)
    if coarse != "pending":
        return coarse
    if current in (None, "pending", "submitted", "unknown"):
        return "accepted"
    return current


def _has_state(state: SqliteState) -> bool:
    return "state" in ledger_columns(state, "orders")


def _reject_never_received(state: SqliteState, client_id: str, now: datetime) -> bool:
    # pending, submitted, accepted and unknown may become rejected: the
    # broker never had it. A pending cancel is left for the next check.
    with_state = _has_state(state)
    state_set = ", state = 'rejected'" if with_state else ""
    not_cancelling = "COALESCE(state, 'pending') <> 'pending_cancel'" if with_state else "1 = 1"
    cur = state.execute(
        f"""
        UPDATE orders
           SET status = 'rejected', status_reason = ?, updated_at = ?{state_set}
         WHERE client_id = ?
           AND status = 'pending'
           AND {not_cancelling}
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


# ---- execution path (roadmap 19.1) ----------------------------------------------


@dataclass(frozen=True)
class ExecutionBooking:
    """What :func:`book_executions` did."""

    fills_inserted: int = 0
    fees_updated: int = 0
    orders_updated: int = 0
    #: Execution ids with no order of this portfolio (a manual trade in a
    #: shared account, or another portfolio's order). Never booked here.
    orphan_executions: tuple[str, ...] = ()


def book_executions(
    state: SqliteState,
    executions: Sequence[Execution],
    *,
    portfolio_id: str = DEFAULT_PORTFOLIO_ID,
    clock: Clock = SYSTEM_CLOCK,
) -> ExecutionBooking:
    """Book ``executions`` as fills of ``portfolio_id``'s orders, one row
    per execution id. A known execution only updates its fee when the
    commission is now known and differs. Orders that gained a fill become
    ``filled`` or ``partially_filled`` from the booked quantity (a broker
    with order state corrects that right after, in :func:`reconcile_orders`)."""
    now = clock.now()
    where, params = ledger_filter(state, "orders", portfolio_id)
    inserted = fees = updated = 0
    orphans: list[str] = []
    touched: set[str] = set()
    for ex in executions:
        rows = state.sql(
            f"SELECT client_id FROM orders WHERE client_id = ? AND {where}",
            [ex.client_id, *params],
        )
        if not rows:
            _log.warning(
                "reconcile.execution_without_order",
                broker_exec_id=ex.broker_exec_id,
                client_id=ex.client_id,
            )
            orphans.append(ex.broker_exec_id)
            continue
        with state.transaction():
            known = _fill_by_exec(state, ex.broker_exec_id, portfolio_id)
            if known is None:
                _insert_fill(state, ex.to_fill(), portfolio_id=portfolio_id)
                inserted += 1
                touched.add(ex.client_id)
            elif ex.commission is not None and (
                abs(float(known["fee"]) - ex.commission) > 1e-12
                or known["fee_currency"] != ex.commission_currency
            ):
                state.execute(
                    "UPDATE fills SET fee = ?, fee_currency = ? WHERE id = ?",
                    [ex.commission, ex.commission_currency, known["id"]],
                )
                fees += 1
    for client_id in sorted(touched):
        updated += _status_from_booked(state, client_id, now)
    return ExecutionBooking(
        fills_inserted=inserted,
        fees_updated=fees,
        orders_updated=updated,
        orphan_executions=tuple(orphans),
    )


def _fill_by_exec(state: SqliteState, exec_id: str, portfolio_id: str) -> Any:
    where, params = ledger_filter(state, "fills", portfolio_id)
    rows = state.sql(
        f"SELECT id, fee, fee_currency FROM fills WHERE broker_exec_id = ? AND {where}",
        [exec_id, *params],
    )
    return rows[0] if rows else None


def _status_from_booked(state: SqliteState, client_id: str, now: datetime) -> int:
    order = state.sql("SELECT quantity FROM orders WHERE client_id = ?", [client_id])[0]
    booked, _ = _booked(state, client_id)
    status: OrderState = (
        "filled" if booked >= float(order["quantity"]) - QTY_EPSILON else "partially_filled"
    )
    current = current_state(state, client_id)
    if current is None or current == status or not can_transition(current, status):
        return 0
    state_set = ", state = ?" if _has_state(state) else ""
    extra = [status] if state_set else []
    cur = state.execute(
        f"UPDATE orders SET status = ?, updated_at = ?{state_set} WHERE client_id = ?",
        [status, _iso(now), *extra, client_id],
    )
    return cur.rowcount


def _reconcile_by_executions(
    broker: ExecutionSource, state: SqliteState, now: datetime, portfolio_id: str
) -> ReconcileSummary:
    # the orders open before booking: one that the executions fill still
    # needs its broker id and final status from the broker
    open_ids = _open_client_ids(state, portfolio_id)
    booking = book_executions(
        state,
        broker.executions(now - EXECUTIONS_LOOKBACK),
        portfolio_id=portfolio_id,
        clock=FixedClock(now),
    )
    if isinstance(broker, OrderStateSource):
        synced = _reconcile_by_order_state(
            broker, state, now, portfolio_id, book_fill=False, client_ids=open_ids
        )
        return ReconcileSummary(
            orders_checked=synced.orders_checked,
            orders_updated=synced.orders_updated + booking.orders_updated,
            fills_inserted=booking.fills_inserted,
            unknown_orders=synced.unknown_orders,
            failed_orders=synced.failed_orders,
            orphan_fills=booking.orphan_executions,
        )
    return ReconcileSummary(
        orders_updated=booking.orders_updated,
        fills_inserted=booking.fills_inserted,
        orphan_fills=booking.orphan_executions,
    )


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


def _insert_fill(state: SqliteState, fill: Fill, *, portfolio_id: str | None = None) -> None:
    # Same timestamp format as production.tick._record_fill so rows written
    # by either path compare equal. The arrival price (TCA, BL-32) of a
    # broker fill is the next session's open: copied from the order when
    # production.tca.refresh_benchmarks already recorded it, else filled in
    # by that refresh later.
    columns = ledger_columns(state, "fills")
    extra = fill_live_values(fill, columns)
    if portfolio_id is not None and "portfolio_id" in columns:
        extra["portfolio_id"] = portfolio_id
    names = ["order_client_id", "ticker", "quantity", "price", "fee", "filled_at", *extra]
    values: list[Any] = [
        fill.order_client_id,
        fill.ticker,
        fill.quantity,
        fill.price,
        fill.fee,
        _iso(fill.filled_at),
        *extra.values(),
    ]
    marks = ["?"] * len(values)
    if "arrival_price" in columns:
        names.append("arrival_price")
        marks.append("(SELECT benchmark_price FROM orders WHERE client_id = ?)")
        values.append(fill.order_client_id)
    state.execute(
        f"INSERT INTO fills ({', '.join(names)}) VALUES ({', '.join(marks)})",
        values,
    )


def order_live_values(order: Order, columns: frozenset[str]) -> dict[str, Any]:
    """The live order columns (migration 028) worth writing for ``order``:
    only values that differ from the default, and only when the column
    exists."""
    values: dict[str, Any] = {}
    if order.stop_price is not None and "stop_price" in columns:
        values["stop_price"] = order.stop_price
    if order.time_in_force is not None and "time_in_force" in columns:
        values["time_in_force"] = order.time_in_force
    if order.outside_rth and "outside_rth" in columns:
        values["outside_rth"] = 1
    return values


def fill_live_values(fill: Fill, columns: frozenset[str]) -> dict[str, Any]:
    """The live fill columns (migration 028) set on ``fill``."""
    values: dict[str, Any] = {}
    if fill.broker_exec_id is not None and "broker_exec_id" in columns:
        values["broker_exec_id"] = fill.broker_exec_id
    if fill.fee_currency is not None and "fee_currency" in columns:
        values["fee_currency"] = fill.fee_currency
    return values


def _iso(ts: datetime) -> str:
    return ts.isoformat(timespec="seconds")


# ---- startup check ----------------------------------------------------------------


@dataclass(frozen=True)
class StartupReconcile:
    """What :func:`startup_reconcile` found."""

    summary: ReconcileSummary
    #: Orders still ``unknown`` after reconciling: nothing may be submitted.
    unresolved: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.unresolved and not self.summary.failed_orders


def startup_reconcile(
    broker: Broker,
    state: SqliteState,
    *,
    portfolio_id: str = DEFAULT_PORTFOLIO_ID,
    clock: Clock = SYSTEM_CLOCK,
) -> StartupReconcile:
    """Reconcile the portfolio before a submit window opens: sync every open
    order (``unknown`` ones included) by client id, then report what is still
    unresolved. The submit waits until :attr:`StartupReconcile.ok`."""
    summary = reconcile_orders(broker, state, now=clock.now(), portfolio_id=portfolio_id)
    return StartupReconcile(summary=summary, unresolved=unknown_orders(state, portfolio_id))
