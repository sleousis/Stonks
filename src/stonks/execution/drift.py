"""Drift between the broker and the ledger (roadmap 19.5).

The broker is the source of truth. These functions compare what it holds
with what the ledger says Stonks owns, and name every difference as a
:class:`DriftItem`. They are pure: the checks in
``production/live/checks.py`` read both sides, reconcile first, and store
the result.

The account is shared with the owner's own trading, so only Stonks' own
positions and orders can drift (ownership by attribution,
``production/ownership.py``):

- **Positions.** Stonks owns its net filled quantity per ticker. The
  broker must hold at least that, on the same side. Anything more is the
  owner's (``external``), never drift. With ``allow_manual = False`` every
  position must match exactly and a ticker Stonks never traded is drift.
- **Orders.** A working order with no reference of ours was placed by hand:
  external, or drift when manual trades are off. A reference the ledger
  never wrote, a working order the ledger closed, and a working ledger
  order the broker does not list are always drift.

Items that are not material (a stuck order at the end of the day, a
missing commission, an order still ``unknown``) raise an alert but never
open a halt.

Roadmap 19.15 adds two comparisons of the end-of-day check:

- **Cash.** The owner's own trades move the same cash, so only the change
  Stonks can explain is compared: its own fills and fees, dividends on its
  positions and any other flow the broker's statement names. What is left
  beyond a tolerance is a ``cash`` or ``settled_cash`` item. It only warns
  unless the owner makes it material.
- **Broker statement.** The statement (IBKR Flex) is the official record.
  Its executions of Stonks' own orders must match the booked fills. A
  missing or extra execution, or a different quantity, is drift. A
  different commission warns.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Literal, cast

from stonks.execution.brokers.base import BrokerOpenOrder, OrderState

__all__ = [
    "DRIFT_KINDS",
    "WORKING",
    "BookedFill",
    "BrokerStatement",
    "CashField",
    "CashFlows",
    "DriftItem",
    "DriftKind",
    "DriftStatus",
    "LedgerOrder",
    "StatementCash",
    "StatementExecution",
    "cash_drift",
    "cash_tolerance",
    "eod_items",
    "order_drift",
    "position_drift",
    "report_status",
    "statement_drift",
]

DriftKind = Literal[
    # material: open the broker_drift halt
    "position_qty",
    "unknown_position",
    "unknown_order",
    "missing_order",
    "order_state",
    "unknown_execution",
    "statement_missing_execution",
    "statement_extra_execution",
    "statement_quantity",
    # not material: alert only
    "unresolved_order",
    "stuck_order",
    "commission_missing",
    "stale_order",
    "statement_commission",
    # alert only, unless ``cash_is_drift`` makes them material
    "cash",
    "settled_cash",
]
DRIFT_KINDS: tuple[str, ...] = (
    "position_qty",
    "unknown_position",
    "unknown_order",
    "missing_order",
    "order_state",
    "unknown_execution",
    "statement_missing_execution",
    "statement_extra_execution",
    "statement_quantity",
    "unresolved_order",
    "stuck_order",
    "commission_missing",
    "stale_order",
    "statement_commission",
    "cash",
    "settled_cash",
)
CashField = Literal["cash", "settled_cash"]
#: ``clean``: nothing unexplained. ``warn``: only items that alert.
#: ``drift``: at least one material item.
DriftStatus = Literal["clean", "warn", "drift"]

#: Ledger states of an order the broker should list as working.
WORKING: frozenset[OrderState] = frozenset(
    {"submitted", "accepted", "partially_filled", "pending_cancel"}
)
_EPS = 1e-9

Value = float | str | None


@dataclass(frozen=True)
class DriftItem:
    """One difference: its kind, the ticker or order, our value and the
    broker's, and whether it counts toward a halt."""

    kind: DriftKind
    #: The ticker, our client id or the broker's order id.
    key: str
    ours: Value
    broker: Value
    material: bool
    explained: bool = False
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DriftItem:
        return cls(
            kind=cast(DriftKind, data["kind"]),
            key=str(data["key"]),
            ours=data.get("ours"),
            broker=data.get("broker"),
            material=bool(data["material"]),
            explained=bool(data.get("explained", False)),
            detail=str(data.get("detail") or ""),
        )


@dataclass(frozen=True)
class LedgerOrder:
    """An order row as the check sees it (after reconciliation)."""

    client_id: str
    ticker: str
    state: OrderState


def position_drift(
    owned: Mapping[str, float],
    broker: Mapping[str, float],
    *,
    allow_manual: bool,
) -> tuple[list[DriftItem], dict[str, float]]:
    """``(items, external quantity per ticker)`` for Stonks' ``owned``
    positions against the broker's ``broker`` positions (both signed)."""
    items: list[DriftItem] = []
    external: dict[str, float] = {}
    for ticker in sorted(set(owned) | set(broker)):
        mine = float(owned.get(ticker, 0.0))
        held = float(broker.get(ticker, 0.0))
        if abs(mine) <= _EPS and abs(held) <= _EPS:
            continue
        if not allow_manual:
            if abs(held - mine) <= _EPS:
                continue
            kind: DriftKind = "position_qty" if abs(mine) > _EPS else "unknown_position"
            items.append(_position_item(kind, ticker, mine, held))
            continue
        if abs(mine) <= _EPS:
            external[ticker] = held
            continue
        covered = mine * held > 0 and abs(held) >= abs(mine) - _EPS
        if not covered:
            items.append(_position_item("position_qty", ticker, mine, held))
            if abs(held) > _EPS and mine * held < 0:
                external[ticker] = held
            continue
        rest = held - mine
        if abs(rest) > _EPS:
            external[ticker] = rest
    return items, external


def _position_item(kind: DriftKind, ticker: str, mine: float, held: float) -> DriftItem:
    if kind == "unknown_position":
        detail = "a position Stonks never traded, and manual trades are off"
    else:
        detail = f"Stonks owns {mine:g}, the broker holds {held:g}"
    return DriftItem(kind=kind, key=ticker, ours=mine, broker=held, material=True, detail=detail)


def order_drift(
    ledger: Sequence[LedgerOrder],
    broker_open: Sequence[BrokerOpenOrder],
    *,
    allow_manual: bool,
) -> tuple[list[DriftItem], list[BrokerOpenOrder]]:
    """``(items, external orders)`` for the portfolio's ledger orders
    against the broker's working orders."""
    by_id = {o.client_id: o for o in ledger}
    items: list[DriftItem] = []
    external: list[BrokerOpenOrder] = []
    working_ids: set[str] = set()
    for order in broker_open:
        if order.client_id is None:
            if allow_manual:
                external.append(order)
            else:
                items.append(
                    DriftItem(
                        kind="unknown_order",
                        key=order.broker_order_id,
                        ours=None,
                        broker=order.quantity,
                        material=True,
                        detail=f"a hand-placed {order.side} of {order.ticker}, "
                        "and manual trades are off",
                    )
                )
            continue
        working_ids.add(order.client_id)
        mine = by_id.get(order.client_id)
        if mine is None:
            items.append(
                DriftItem(
                    kind="unknown_order",
                    key=order.client_id,
                    ours=None,
                    broker=order.quantity,
                    material=True,
                    detail=f"a working {order.side} of {order.ticker} with a reference "
                    "the ledger never wrote",
                )
            )
        elif mine.state not in WORKING and mine.state not in ("pending", "unknown"):
            items.append(
                DriftItem(
                    kind="order_state",
                    key=order.client_id,
                    ours=mine.state,
                    broker="working",
                    material=True,
                    detail=f"the ledger has it {mine.state}, the broker still works it",
                )
            )
    for mine in ledger:
        if mine.state == "unknown":
            items.append(
                DriftItem(
                    kind="unresolved_order",
                    key=mine.client_id,
                    ours="unknown",
                    broker=None,
                    material=False,
                    detail="its outcome is still unknown: nothing is sent until it resolves",
                )
            )
        elif mine.state in WORKING and mine.client_id not in working_ids:
            items.append(
                DriftItem(
                    kind="missing_order",
                    key=mine.client_id,
                    ours=mine.state,
                    broker=None,
                    material=True,
                    detail=f"the ledger works a {mine.ticker} order the broker does not list",
                )
            )
    return items, external


def eod_items(
    *, stuck: Iterable[LedgerOrder], missing_commission: Iterable[str]
) -> list[DriftItem]:
    """The end-of-day findings that alert: orders sent today that are not
    terminal, and executions booked with no commission yet."""
    items = [
        DriftItem(
            kind="stuck_order",
            key=o.client_id,
            ours=o.state,
            broker=None,
            material=False,
            detail=f"a {o.ticker} order sent today is still {o.state} after the close",
        )
        for o in stuck
    ]
    items += [
        DriftItem(
            kind="commission_missing",
            key=exec_id,
            ours=None,
            broker=None,
            material=False,
            detail="an execution has no commission yet (booked at the next check)",
        )
        for exec_id in missing_commission
    ]
    return sorted(items, key=lambda i: (i.kind, i.key))


# ---- cash (roadmap 19.15) --------------------------------------------------------------


@dataclass(frozen=True)
class CashFlows:
    """The cash movements over a window that Stonks can explain, in the
    account's base currency. Money in is positive."""

    #: Stonks' own fills: a buy pays, a sale receives.
    trades: float = 0.0
    #: Stonks' own commissions (a cost is negative).
    fees: float = 0.0
    #: Dividends on Stonks' positions.
    dividends: float = 0.0
    #: Other flows the broker's statement names: the owner's own trades,
    #: dividends on the owner's holdings, interest, deposits, fees.
    external: float = 0.0

    @property
    def total(self) -> float:
        return self.trades + self.fees + self.dividends + self.external


def cash_tolerance(equity: float, *, minimum: float, fraction: float) -> float:
    """The larger of a floor in currency units and a fraction of the
    account's net liquidation value."""
    return max(minimum, fraction * abs(equity))


def cash_drift(
    field: CashField,
    currency: str,
    before: float,
    after: float,
    flows: CashFlows,
    *,
    tolerance: float,
    material: bool = False,
) -> list[DriftItem]:
    """One item when the broker's change in ``field`` (``after - before``)
    differs from what Stonks explains by more than ``tolerance``."""
    change = after - before
    residual = change - flows.total
    if abs(residual) <= tolerance + _EPS:
        return []
    label = "settled cash" if field == "settled_cash" else "cash"
    detail = (
        f"the broker's {label} moved {change:+.2f} {currency}, Stonks explains "
        f"{flows.total:+.2f} (trades {flows.trades:+.2f}, fees {flows.fees:+.2f}, "
        f"dividends {flows.dividends:+.2f}, other known flows {flows.external:+.2f}), "
        f"{residual:+.2f} unexplained"
    )
    item = DriftItem(
        kind=field, key=currency, ours=flows.total, broker=change, material=material, detail=detail
    )
    return [item]


# ---- broker statement (roadmap 19.15) ----------------------------------------------------


@dataclass(frozen=True)
class StatementExecution:
    """One execution in the broker's official statement, vendor free.
    ``quantity`` is signed (a sale is negative). ``commission`` is a cost
    (positive). ``cash`` is the trade's signed cash before commission."""

    exec_id: str
    quantity: float
    symbol: str = ""
    order_ref: str | None = None
    trade_date: date | None = None
    settle_date: date | None = None
    cash: float | None = None
    commission: float | None = None
    commission_currency: str | None = None
    currency: str | None = None


@dataclass(frozen=True)
class StatementCash:
    """One cash movement in the statement. ``kind`` is ``dividend`` for
    dividends and payments in lieu, else ``other``."""

    kind: Literal["dividend", "other"]
    amount: float
    date: date | None
    settle_date: date | None = None
    currency: str | None = None
    symbol: str | None = None


@dataclass(frozen=True)
class BrokerStatement:
    """The broker's official record of an account from ``from_date`` to
    ``to_date``, both included."""

    account_id: str
    from_date: date | None
    to_date: date | None
    executions: tuple[StatementExecution, ...] = ()
    cash: tuple[StatementCash, ...] = ()


@dataclass(frozen=True)
class BookedFill:
    """A fill the ledger booked from a broker execution. ``quantity`` is
    signed. ``fee`` is ``None`` while no commission is booked."""

    exec_id: str
    ticker: str
    quantity: float
    fee: float | None
    fee_currency: str | None


def statement_drift(
    executions: Sequence[StatementExecution],
    booked: Sequence[BookedFill],
    *,
    commission_tolerance: float,
) -> list[DriftItem]:
    """The statement's executions of Stonks' own orders against the fills
    the ledger booked over the same days."""
    listed = {e.exec_id: e for e in executions}
    mine = {f.exec_id: f for f in booked}
    items: list[DriftItem] = []
    for exec_id, e in listed.items():
        f = mine.get(exec_id)
        if f is None:
            items.append(
                DriftItem(
                    kind="statement_missing_execution",
                    key=exec_id,
                    ours=None,
                    broker=e.quantity,
                    material=True,
                    detail=f"the statement lists an execution of {e.symbol or 'an order'} "
                    "of ours that the ledger never booked",
                )
            )
            continue
        if abs(f.quantity - e.quantity) > _EPS:
            items.append(
                DriftItem(
                    kind="statement_quantity",
                    key=exec_id,
                    ours=f.quantity,
                    broker=e.quantity,
                    material=True,
                    detail=f"the ledger booked {f.quantity:g} {f.ticker}, "
                    f"the statement says {e.quantity:g}",
                )
            )
        items += _commission_item(e, f, commission_tolerance)
    for exec_id, f in mine.items():
        if exec_id not in listed:
            items.append(
                DriftItem(
                    kind="statement_extra_execution",
                    key=exec_id,
                    ours=f.quantity,
                    broker=None,
                    material=True,
                    detail=f"the ledger booked a {f.ticker} fill the statement does not list",
                )
            )
    return sorted(items, key=lambda i: (not i.material, i.kind, i.key))


def _commission_item(e: StatementExecution, f: BookedFill, tolerance: float) -> list[DriftItem]:
    if e.commission is None:
        return []
    if f.fee is None:
        detail = f"no commission booked yet, the statement says {e.commission:g}"
    elif (
        e.commission_currency and f.fee_currency and e.commission_currency != f.fee_currency
    ) or abs(e.commission - f.fee) <= tolerance + _EPS:
        return []
    else:
        detail = f"the ledger booked a commission of {f.fee:g}, the statement says {e.commission:g}"
    item = DriftItem(
        kind="statement_commission",
        key=e.exec_id,
        ours=f.fee,
        broker=e.commission,
        material=False,
        detail=detail,
    )
    return [item]


def report_status(items: Iterable[DriftItem]) -> DriftStatus:
    open_items = [i for i in items if not i.explained]
    if any(i.material for i in open_items):
        return "drift"
    return "warn" if open_items else "clean"
