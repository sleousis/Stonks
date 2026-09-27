"""Manual orders (roadmap 20.1): a person places, changes or cancels an
order on one of their portfolios, next to what the strategies do.

A manual order takes the same road as a tick's orders, minus the signal:

1. **Idempotent by client id.** The caller may name a key; the ledger id is
   ``manual:<portfolio>:<key>``. Placing the same key again returns the
   recorded order and places nothing. The same key for a different order
   is refused.
2. **Halts.** Every trade gate (the kill switch, breaker trips, the
   operational halt) is read for the portfolio, its owner and a paper
   account's broker portfolio. ``all`` refuses every order, ``buys``
   refuses orders that do not reduce a position. The gates run as a dry
   run: an order request never writes a breaker trip.
3. **Every risk rule** of the book's policy (``apply_risk``, the account
   rules and live safeguards included once they are registered rules),
   against the whole book at the latest closes. A rule that would drop the
   order refuses it. A rule that would shrink it refuses it too, unless the
   caller allowed a smaller order (``allow_reduce``): a person never gets a
   different order than the one they asked for by surprise.
4. **Execution.** A simulated book fills at once through the simulated
   broker at the latest close (a limit order only when that close is at or
   better than its limit, else it is recorded as rejected), and the fill,
   the order and a new ledger snapshot are written in one transaction. A
   book at a broker goes through the broker seam: the order row is
   committed ``pending`` before the submit and synced by client id after,
   exactly like the tick (``reconcile_order``).

Every manual order is recorded with ``origin = 'manual'``, no strategy, who
placed it and why (migration 027), and an ``audit_log`` row. The tick never
trades manual holdings (``ownership.manual_positions``).

A change is a cancel and a replace: the working order is cancelled at the
broker, then a new order (``<first id>.r<n>``, ``replaces_client_id`` set)
goes through the whole road again. Only orders still working can change,
so on a simulated book (which fills at once) nothing can.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from typing import Any, Literal

from stonks.accounts.audit import AuditLog
from stonks.backtest.costs import Trade
from stonks.config import RiskPolicy
from stonks.core.protocols import Broker
from stonks.core.types import Fill, Order, OrderSide, OrderStatus, Portfolio
from stonks.execution.brokers.base import OrderCanceller, OrderRejectedError, OrderStateSource
from stonks.execution.order_state import mark_unknown, unknown_orders, write_state
from stonks.execution.orders import classify_all
from stonks.execution.reconcile import NON_TERMINAL_STATUSES, reconcile_order, reconcile_orders
from stonks.logging import get_logger
from stonks.production.hooks import GateContext, run_gates
from stonks.production.live.context import LiveContext
from stonks.production.prices import load_prices
from stonks.production.risk import apply_risk, build_risk_context, needs_risk_context
from stonks.production.rules import RiskContext
from stonks.production.tca import expected_cost_bps
from stonks.production.tick import (
    TickSettings,
    _asset_classes,
    _load_or_seed_portfolio,
    _record_fill,
    _record_order,
    _snapshot_portfolio,
)
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState

_log = get_logger("stonks.production.manual")

MANUAL_ORIGIN = "manual"
ManualOrderType = Literal["market", "limit"]
_QTY_EPS = 1e-9
#: The suffix a replacement order's client id carries (``.r1``, ``.r2``, ...).
_REPLACED = re.compile(r"\.r\d+$")


class ManualOrderRefused(Exception):
    """The order was not placed. ``adjustments`` lists what the risk rules
    would have done (each ``RiskAdjustment.as_dict``)."""

    def __init__(self, message: str, *, adjustments: tuple[dict[str, Any], ...] = ()) -> None:
        super().__init__(message)
        self.adjustments = adjustments


class ManualOrderNotFound(LookupError):
    """No such order in that portfolio."""


@dataclass(frozen=True)
class ManualOrder:
    portfolio_id: str
    ticker: str
    side: OrderSide
    quantity: float
    #: Why the person trades (recorded on the order and in the audit log).
    reason: str
    #: Who placed it (``user:<id>`` or ``service:<name>``).
    actor: str
    order_type: ManualOrderType = "market"
    limit_price: float | None = None
    #: The caller's idempotency key; a random one when ``None``.
    client_key: str | None = None
    #: Place a smaller order when a risk rule shrinks it (else refuse).
    allow_reduce: bool = False
    #: The client id of the order this one replaces (a change).
    replaces: str | None = None


@dataclass(frozen=True)
class ManualBook:
    """The portfolio a manual order trades in, as the tick would see it."""

    portfolio_id: str
    owner_id: str | None
    risk: RiskPolicy
    initial_cash: float
    allow_short: bool = False
    #: A paper account's broker portfolio (its halts stop this book too).
    parent_id: str | None = None
    #: The broker of a book that trades at one; ``None``: simulated fills
    #: on the Stonks ledger.
    broker: Broker | None = None


@dataclass(frozen=True)
class ManualResult:
    client_id: str
    #: The ledger status, or ``preview`` for a dry run.
    status: OrderStatus | Literal["preview"]
    requested_quantity: float
    #: The quantity placed (after risk, when ``allow_reduce``).
    quantity: float
    #: The latest close the order was checked and priced at.
    reference_price: float
    #: The average fill price, when it filled.
    fill_price: float | None = None
    reason: str | None = None
    adjustments: tuple[dict[str, Any], ...] = ()
    #: The client id was placed before; nothing new was sent.
    duplicate: bool = False
    #: A halt that limits the book (its reason), even when the order passed.
    halt: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)


def manual_client_id(portfolio_id: str, key: str | None = None) -> str:
    """``manual:<portfolio>:<key>``, with a random key when none is given."""
    return f"manual:{portfolio_id}:{key or uuid.uuid4().hex[:16]}"


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---- place ---------------------------------------------------------------------------


def place_manual_order(
    state: SqliteState,
    lake: DuckDBLake,
    order: ManualOrder,
    book: ManualBook,
    tick: TickSettings,
    *,
    preview: bool = False,
    now: datetime | None = None,
) -> ManualResult:
    """Check and place ``order`` (see the module doc). ``preview`` runs every
    check and returns what would be placed without writing or sending
    anything. Raises :class:`ManualOrderRefused` when a check fails."""
    now = now or _utcnow()
    as_of = now.date()
    client_id = (
        manual_client_id(book.portfolio_id, order.client_key)
        if order.replaces is None
        else _successor_id(state, order.replaces)
    )
    existing = _existing(state, client_id)
    if existing is not None:
        return _duplicate(existing, order, book.portfolio_id)
    if _tick_running(state):
        raise ManualOrderRefused("a tick is running now; try again when it has finished")
    if order.quantity <= 0:
        raise ManualOrderRefused("the quantity must be positive")
    if order.order_type == "limit" and (order.limit_price is None or order.limit_price <= 0):
        raise ManualOrderRefused("a limit order needs a positive limit price")

    external = book.broker is not None
    if external:
        assert book.broker is not None
        if not isinstance(book.broker, OrderStateSource):
            raise ManualOrderRefused(
                "this book's broker cannot look orders up by client id, so a manual order "
                "could not be reconciled"
            )
        if not preview:
            reconcile_orders(book.broker, state, portfolio_id=book.portfolio_id)
        # 19.8: the reconciliation gate. While an order of the book is still
        # ``unknown`` at the broker, nothing new is sent (as in the tick).
        unresolved = unknown_orders(state, book.portfolio_id)
        if unresolved:
            raise ManualOrderRefused(
                f"{len(unresolved)} order(s) of this book are in an unknown state at the "
                "broker; wait until they are reconciled"
            )
        portfolio = book.broker.fetch_portfolio()
    else:
        portfolio = _load_or_seed_portfolio(state, book.initial_cash, book.portfolio_id)

    held = [t for t, q in portfolio.positions.items() if abs(q) > _QTY_EPS]
    priced = load_prices(
        lake, [order.ticker], held, as_of, max_staleness_days=tick.max_price_staleness_days
    )
    price = priced.prices.get(order.ticker)
    if price is None or order.ticker not in priced.fresh or price <= 0:
        raise ManualOrderRefused(
            f"no recent close for {order.ticker} in the lake "
            f"(within {tick.max_price_staleness_days} days)"
        )

    proposed = Order(
        client_id=client_id,
        ticker=order.ticker,
        side=order.side,
        quantity=float(order.quantity),
        order_type=order.order_type,
        limit_price=order.limit_price if order.order_type == "limit" else None,
        portfolio_id=book.portfolio_id,
    )
    if book.allow_short:
        legs = classify_all([proposed], portfolio.positions)
        if len(legs) > 1:
            raise ManualOrderRefused(
                "this order would close a position and open the other way; place the close "
                "and the new position as two orders"
            )
        proposed = legs[0]

    verdict = run_gates(
        GateContext(
            state=state,
            as_of=as_of,
            portfolio_id=book.portfolio_id,
            owner_id=book.owner_id,
            dry_run=True,
            policy=book.risk,
            parent_portfolio_id=book.parent_id,
        ),
        _log,
    )
    halt_reason = verdict.reason if verdict is not None else None
    if verdict is not None and (verdict.halt == "all" or not _reduces(proposed, portfolio)):
        raise ManualOrderRefused(f"trading is halted for this portfolio: {verdict.reason}")

    asset_classes = _asset_classes(lake, [order.ticker, *held])
    prices = dict(priced.prices)
    context = None
    if needs_risk_context(book.risk):
        context = build_risk_context(
            lake,
            state,
            portfolio,
            prices,
            as_of,
            policy=book.risk,
            universe=[order.ticker],
            cost_model=tick.costs,
            volumes=priced.volumes,
            portfolio_id=book.portfolio_id,
        )
    if external:
        # 19.8: a book at a real broker gives the live safeguards and the
        # account rules their live context, as the tick does. They do
        # nothing without it.
        context = replace(
            context
            or RiskContext(
                portfolio=portfolio,
                prices=prices,
                asset_classes=asset_classes,
                policy=book.risk,
                portfolio_id=book.portfolio_id,
                as_of=as_of,
                allow_short=book.allow_short,
            ),
            live=_live_context(state, lake, book, as_of, sorted({order.ticker, *held})),
        )
    costs: dict[str, Any] = (
        {"cost_model": tick.costs}
        if tick.costs is not None
        else {"slippage_bps": tick.slippage_bps, "fee_per_trade": tick.fee_per_trade}
    )
    risk = apply_risk(
        [proposed],
        portfolio,
        prices,
        asset_classes,
        book.risk,
        volumes=priced.volumes,
        context=context,
        allow_short=book.allow_short,
        **costs,
    )
    adjustments = tuple(a.as_dict() for a in risk.adjustments)
    if not risk.orders:
        raise ManualOrderRefused(
            "the risk rules refuse this order: " + _explain(adjustments), adjustments=adjustments
        )
    allowed = risk.orders[0]
    if allowed.quantity < proposed.quantity - _QTY_EPS and not order.allow_reduce:
        raise ManualOrderRefused(
            f"the risk rules allow {allowed.quantity:g} of the {proposed.quantity:g} asked for: "
            f"{_explain(adjustments)}. Place the smaller order, or allow a reduced order.",
            adjustments=adjustments,
        )

    decided = replace(
        allowed,
        decision_price=float(price),
        decided_at=now,
        decision_context={
            "trigger": MANUAL_ORIGIN,
            "reason": order.reason,
            "placed_by": order.actor,
        },
        expected_cost_bps=_expected_cost(tick, allowed, float(price), asset_classes, priced),
    )
    result = ManualResult(
        client_id=client_id,
        status="preview",
        requested_quantity=float(order.quantity),
        quantity=decided.quantity,
        reference_price=float(price),
        adjustments=adjustments,
        halt=halt_reason,
    )
    if preview:
        return result
    if external:
        assert book.broker is not None
        return _place_at_broker(state, book, order, decided, result)
    return _fill_simulated(
        state, book, order, decided, result, portfolio, priced, asset_classes, tick, now
    )


def _fill_simulated(
    state: SqliteState,
    book: ManualBook,
    order: ManualOrder,
    decided: Order,
    result: ManualResult,
    portfolio: Portfolio,
    priced: Any,
    asset_classes: Mapping[str, str],
    tick: TickSettings,
    now: datetime,
) -> ManualResult:
    price = result.reference_price
    status: OrderStatus = "filled"
    reason: str | None = None
    fill: Fill | None = None
    arrival: float | None = None
    if decided.order_type == "limit" and not _marketable(decided, price):
        status = "rejected"
        reason = (
            f"limit {decided.limit_price:g} is not marketable at the latest close {price:g}; "
            "a simulated book fills at once or not at all"
        )
    else:
        broker = tick.simulated_costs.build_broker(portfolio)
        broker.set_asset_classes(dict(asset_classes))  # type: ignore[arg-type]
        broker.set_prices(priced.prices, as_of=now.date(), volumes=priced.volumes)
        # The simulated broker fills market orders; a marketable limit
        # fills the same way at the close.
        fill = broker.place_order(replace(decided, order_type="market", limit_price=None))
        if fill is None:
            status = "rejected"
            reason = "not enough cash for this order at the latest close"
        else:
            if fill.quantity < decided.quantity - _QTY_EPS:
                reason = f"filled {fill.quantity:g} of {decided.quantity:g} requested"
                decided = replace(decided, quantity=fill.quantity)
            reference = broker.reference_price(decided.client_id)
            arrival = float(reference) if reference is not None else None
    with state.transaction():
        _record_order(state, decided, status=status, reason=reason, portfolio_id=book.portfolio_id)
        _mark_manual(state, decided.client_id, order)
        if fill is not None:
            _record_fill(state, fill, portfolio_id=book.portfolio_id, arrival_price=arrival)
            _snapshot_portfolio(
                state, None, portfolio, priced.prices, now.date(), portfolio_id=book.portfolio_id
            )
        _audit(state, order, decided, status, book.portfolio_id)
    _log.info(
        "manual_order.placed",
        client_id=decided.client_id,
        portfolio_id=book.portfolio_id,
        ticker=decided.ticker,
        side=decided.side,
        quantity=decided.quantity,
        status=status,
    )
    return replace(
        result,
        status=status,
        quantity=decided.quantity,
        fill_price=fill.price if fill is not None else None,
        reason=reason,
    )


def _place_at_broker(
    state: SqliteState, book: ManualBook, order: ManualOrder, decided: Order, result: ManualResult
) -> ManualResult:
    """The row is committed ``pending`` before the broker sees the order,
    then synced by client id (see ``tick.place_external``)."""
    broker = book.broker
    assert broker is not None and isinstance(broker, OrderStateSource)
    with state.transaction():
        _record_order(state, decided, status="pending", portfolio_id=book.portfolio_id)
        _mark_manual(state, decided.client_id, order)
        _audit(state, order, decided, "pending", book.portfolio_id)
    try:
        broker.place_order(decided)
    except OrderRejectedError as exc:
        _record_order(
            state, decided, status="rejected", reason=str(exc), portfolio_id=book.portfolio_id
        )
        return replace(result, status="rejected", reason=str(exc))
    except Exception as exc:
        # It may or may not have reached the broker: the row turns
        # ``unknown`` and the reconciliation gate stays shut until the next
        # reconcile settles it by client id (19.8, as in the tick).
        _log.warning("manual_order.submit_failed", client_id=decided.client_id, error=str(exc))
        mark_unknown(state, decided.client_id, f"submit outcome unknown: {exc}"[:500])
        return replace(
            result,
            status="pending",
            reason="the broker did not confirm the order; it is checked again at the next sync",
        )
    write_state(state, decided.client_id, "submitted")
    try:
        reconcile_order(broker, state, decided.client_id, reject_unknown=False)
    except Exception as exc:
        _log.warning("manual_order.sync_failed", client_id=decided.client_id, error=str(exc))
    row = _existing(state, decided.client_id)
    assert row is not None
    return replace(
        result,
        status=row["status"],
        reason=row["status_reason"],
        fill_price=_avg_fill(state, decided.client_id),
    )


def _live_context(
    state: SqliteState, lake: Any, book: ManualBook, as_of: date, tickers: list[str]
) -> LiveContext:
    """What the live safeguards see of ``book`` (never raises: a context that
    cannot be built is empty, and the rules then refuse to open)."""
    from stonks.production.live.context import build_live_context
    from stonks.production.rules._common import settings_of

    try:
        return build_live_context(
            state,
            book.portfolio_id,
            as_of,
            broker=book.broker,
            tickers=tickers,
            lake=lake,
            account_settings=settings_of(book.risk, "account_rules"),
        )
    except Exception as exc:
        _log.error(
            "manual_order.live_context_failed", portfolio_id=book.portfolio_id, error=str(exc)
        )
        return LiveContext(portfolio_id=book.portfolio_id)


# ---- cancel and change -----------------------------------------------------------------


@dataclass(frozen=True)
class CancelResult:
    client_id: str
    status: str
    cancelled: bool


def cancel_order(
    state: SqliteState,
    portfolio_id: str,
    client_id: str,
    *,
    broker: Broker | None,
    actor: str,
    reason: str,
) -> CancelResult:
    """Cancel one working order of ``portfolio_id`` (a manual one or a
    strategy's). Refused when it is no longer working or the book has no
    broker to cancel at (a simulated book fills at once)."""
    row = _order_row(state, portfolio_id, client_id)
    if row["status"] not in NON_TERMINAL_STATUSES:
        raise ManualOrderRefused(f"order {client_id} is {row['status']}; only a working order")
    if not isinstance(broker, OrderCanceller) or not isinstance(broker, OrderStateSource):
        raise ManualOrderRefused("this book's broker cannot cancel orders")
    accepted = broker.cancel_order(client_id)
    reconcile_order(broker, state, client_id, reject_unknown=False)
    with state.transaction():
        if accepted:
            state.execute(
                "UPDATE orders SET status_reason = ? WHERE client_id = ?",
                [f"cancelled by {actor}: {reason}", client_id],
            )
        AuditLog(state).record(
            actor,
            "order.cancel",
            "order",
            client_id,
            portfolio_id=portfolio_id,
            details={"reason": reason, "accepted": accepted},
        )
    status = str(_order_row(state, portfolio_id, client_id)["status"])
    _log.info("manual_order.cancel", client_id=client_id, accepted=accepted, status=status)
    return CancelResult(client_id=client_id, status=status, cancelled=accepted)


def change_manual_order(
    state: SqliteState,
    lake: DuckDBLake,
    client_id: str,
    book: ManualBook,
    tick: TickSettings,
    *,
    actor: str,
    reason: str,
    quantity: float | None = None,
    limit_price: float | None = None,
    allow_reduce: bool = False,
    now: datetime | None = None,
) -> ManualResult:
    """Cancel the working manual order ``client_id`` and place its
    replacement with the new quantity or limit (every check again)."""
    row = _order_row(state, book.portfolio_id, client_id)
    if row["origin"] != MANUAL_ORIGIN:
        raise ManualOrderRefused("only a manual order can change; cancel a strategy's order")
    if row["status"] not in NON_TERMINAL_STATUSES:
        raise ManualOrderRefused(f"order {client_id} is {row['status']}; only a working order")
    if quantity is None and limit_price is None:
        raise ManualOrderRefused("give a new quantity or a new limit price")
    if limit_price is not None and row["order_type"] != "limit":
        raise ManualOrderRefused("only a limit order has a limit price to change")
    filled = _filled_quantity(state, client_id)
    new_qty = float(quantity if quantity is not None else row["quantity"]) - filled
    if new_qty <= _QTY_EPS:
        raise ManualOrderRefused(f"{filled:g} already filled; the new quantity must be larger")
    cancelled = cancel_order(
        state, book.portfolio_id, client_id, broker=book.broker, actor=actor, reason=reason
    )
    if not cancelled.cancelled:
        raise ManualOrderRefused(
            f"the broker did not cancel {client_id} (it is {cancelled.status}); nothing replaced it"
        )
    if cancelled.status != "cancelled":
        # The cancel was only requested: the original can still fill, so a
        # replacement now could double the position.
        raise ManualOrderRefused(
            f"the cancel of {client_id} is not confirmed yet (it is {cancelled.status}); "
            "change it again once it shows cancelled"
        )
    filled = _filled_quantity(state, client_id)
    new_qty = float(quantity if quantity is not None else row["quantity"]) - filled
    if new_qty <= _QTY_EPS:
        raise ManualOrderRefused(f"{filled:g} filled before the cancel; nothing replaced it")
    return place_manual_order(
        state,
        lake,
        ManualOrder(
            portfolio_id=book.portfolio_id,
            ticker=row["ticker"],
            side=row["side"],
            quantity=new_qty,
            reason=reason,
            actor=actor,
            order_type=row["order_type"],
            limit_price=limit_price if limit_price is not None else row["limit_price"],
            allow_reduce=allow_reduce,
            replaces=client_id,
        ),
        book,
        tick,
        now=now,
    )


# ---- helpers -----------------------------------------------------------------------


def _existing(state: SqliteState, client_id: str) -> Any | None:
    rows = state.sql("SELECT * FROM orders WHERE client_id = ?", [client_id])
    return rows[0] if rows else None


def _order_row(state: SqliteState, portfolio_id: str, client_id: str) -> Any:
    rows = state.sql(
        "SELECT * FROM orders WHERE client_id = ? AND portfolio_id = ?", [client_id, portfolio_id]
    )
    if not rows:
        raise ManualOrderNotFound(f"no order {client_id!r} in portfolio {portfolio_id}")
    return rows[0]


def _duplicate(row: Any, order: ManualOrder, portfolio_id: str) -> ManualResult:
    same = (
        row["portfolio_id"] == portfolio_id
        and row["origin"] == MANUAL_ORIGIN
        and row["ticker"] == order.ticker
        and row["side"] == order.side
        and row["order_type"] == order.order_type
    )
    if not same:
        raise ManualOrderRefused(
            f"client id {row['client_id']!r} was already used for another order"
        )
    return ManualResult(
        client_id=row["client_id"],
        status=row["status"],
        requested_quantity=float(order.quantity),
        quantity=float(row["quantity"]),
        reference_price=float(row["decision_price"] or 0.0),
        reason=row["status_reason"],
        duplicate=True,
    )


def _successor_id(state: SqliteState, replaced: str) -> str:
    base = _REPLACED.sub("", replaced)
    rows = state.sql(
        "SELECT client_id FROM orders WHERE substr(client_id, 1, ?) = ?",
        [len(base) + 2, f"{base}.r"],
    )
    return f"{base}.r{len(rows) + 1}"


def _tick_running(state: SqliteState) -> bool:
    return bool(state.sql("SELECT 1 FROM tick_runs WHERE status = 'running' LIMIT 1"))


def _reduces(order: Order, portfolio: Portfolio) -> bool:
    """Whether ``order`` shrinks the position it trades (never flips it)."""
    held = float(portfolio.positions.get(order.ticker, 0.0))
    if order.side == "sell":
        return held > _QTY_EPS and order.quantity <= held + _QTY_EPS
    return held < -_QTY_EPS and order.quantity <= -held + _QTY_EPS


def _marketable(order: Order, price: float) -> bool:
    limit = order.limit_price
    if limit is None:
        return True
    return price <= limit if order.side == "buy" else price >= limit


def _explain(adjustments: tuple[dict[str, Any], ...]) -> str:
    reasons = [str(a.get("reason") or a.get("rule") or "") for a in adjustments]
    reasons = [r for r in reasons if r]
    return "; ".join(reasons) or "no rule gave a reason"


def _expected_cost(
    tick: TickSettings,
    order: Order,
    price: float,
    asset_classes: Mapping[str, str],
    priced: Any,
) -> float | None:
    try:
        return expected_cost_bps(
            tick.cost_model,
            Trade(
                ticker=order.ticker,
                side=order.side,
                quantity=order.quantity,
                price=price,
                asset_class=asset_classes.get(order.ticker, "equity"),  # type: ignore[arg-type]
                bar_volume=priced.volumes.get(order.ticker),
            ),
        )
    except Exception:  # pragma: no cover - the estimate is informative only
        return None


def _mark_manual(state: SqliteState, client_id: str, order: ManualOrder) -> None:
    state.execute(
        "UPDATE orders SET origin = ?, manual_reason = ?, placed_by = ?, replaces_client_id = ?"
        " WHERE client_id = ?",
        [MANUAL_ORIGIN, order.reason, order.actor, order.replaces, client_id],
    )


def _audit(
    state: SqliteState, order: ManualOrder, placed: Order, status: str, portfolio_id: str
) -> None:
    AuditLog(state).record(
        order.actor,
        "order.change" if order.replaces else "order.place",
        "order",
        placed.client_id,
        portfolio_id=portfolio_id,
        details={
            "ticker": placed.ticker,
            "side": placed.side,
            "quantity": placed.quantity,
            "order_type": placed.order_type,
            "limit_price": placed.limit_price,
            "reason": order.reason,
            "status": status,
            "replaces": order.replaces,
        },
    )


def _filled_quantity(state: SqliteState, client_id: str) -> float:
    rows = state.sql(
        "SELECT COALESCE(SUM(quantity), 0) AS q FROM fills WHERE order_client_id = ?", [client_id]
    )
    return float(rows[0]["q"])


def _avg_fill(state: SqliteState, client_id: str) -> float | None:
    rows = state.sql(
        "SELECT SUM(quantity) AS q, SUM(quantity * price) AS n FROM fills WHERE order_client_id = ?",
        [client_id],
    )
    q = rows[0]["q"]
    return float(rows[0]["n"]) / float(q) if q else None
