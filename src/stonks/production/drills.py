"""The kill switch drill (roadmap 19.11, ``docs/design/live-trading.md``
section 7, "Kill switch drills").

:func:`run_kill_switch_drill` proves, step by step, that the global kill
switch stops new orders and cancels the working ones through the broker
seam:

1. ``place_working_order``: one tiny buy limit far below the market, so it
   cannot fill, is written to the ledger and placed at the broker;
2. ``engage_kill``: the global kill switch in stop-all mode (``engage``,
   by default :func:`engage_global_kill`: the halt row plus
   :func:`stonks.execution.cancel.cancel_working_orders`, the same calls
   ``HaltService.engage_kill`` makes);
3. ``new_orders_blocked``: the trade gates now place nothing;
4. ``working_order_cancelled``: the broker reports the drill order
   cancelled within ``cancel_timeout`` seconds and the ledger follows;
5. ``global_cancel``: ``cancel_all`` ran when the broker offers it;
6. ``resume``: the drill's halt is cleared.

The drill runs on a scratch state DB (the CLI makes a fresh one), and
refuses a broker in ``live`` mode, a broker that cannot cancel or look up
orders, and a state that already has a halt in force. Whatever fails, the
drill cancels its order and clears its halt before it returns.

:class:`SimulatedWorkingBroker` is the default broker: an in-memory book
that keeps limit orders working until they cross, so there is something to
cancel. It never talks to a real broker.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

from stonks.accounts.models import DEFAULT_PORTFOLIO_ID
from stonks.core.types import Fill, Order
from stonks.execution.brokers.base import (
    BrokerOrderState,
    GlobalCanceller,
    OrderCanceller,
    OrderStateSource,
)
from stonks.execution.cancel import cancel_working_orders
from stonks.execution.reconcile import reconcile_order
from stonks.logging import get_logger
from stonks.production.halts import clear_halt, get_halt, list_halts, trip_halt
from stonks.production.hooks import GateContext, run_gates
from stonks.store.state import SqliteState

__all__ = [
    "DRILL_ACTOR",
    "DRILL_REASON",
    "DrillRefusedError",
    "DrillReport",
    "DrillStep",
    "SimulatedWorkingBroker",
    "engage_global_kill",
    "run_kill_switch_drill",
]

_log = get_logger("stonks.production.drills")

DRILL_ACTOR = "service:drill"
DRILL_REASON = "kill switch drill"
#: The drill order's limit as a share of the reference price.
FAR_FROM_MARKET = 0.5

#: Engage the kill switch on a state and return the halt id.
EngageKill = Callable[[SqliteState], int]


class DrillRefusedError(RuntimeError):
    """The drill will not run here (live broker, missing capability, a halt
    already in force)."""


@dataclass(frozen=True)
class DrillStep:
    name: str
    ok: bool
    detail: str
    elapsed_ms: float


@dataclass(frozen=True)
class DrillReport:
    broker: str
    portfolio_id: str
    client_id: str
    started_at: datetime
    finished_at: datetime
    steps: tuple[DrillStep, ...]
    halt_id: int | None = None
    #: Seconds from engaging the kill switch to the broker reporting the
    #: drill order cancelled (``None`` when it never did).
    cancel_seconds: float | None = None

    @property
    def passed(self) -> bool:
        return bool(self.steps) and all(s.ok for s in self.steps)

    def step(self, name: str) -> DrillStep | None:
        return next((s for s in self.steps if s.name == name), None)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["started_at"] = self.started_at.isoformat()
        data["finished_at"] = self.finished_at.isoformat()
        data["passed"] = self.passed
        return data


# ---- the default broker ----------------------------------------------------------------


@dataclass
class _Working:
    order: Order
    state: str = "accepted"
    filled: float = 0.0
    price: float | None = None


@dataclass
class SimulatedWorkingBroker:
    """An in-memory broker that keeps orders working. A limit order rests
    until the market price (``prices``) crosses it, and an order for a
    ticker with no price rests too. Market orders fill at the price.
    Idempotent by client id, like every broker."""

    prices: Mapping[str, float] = field(default_factory=dict[str, float])
    broker_kind = "simulated"
    orders: dict[str, Order] = field(default_factory=dict[str, Order])
    _book: dict[str, _Working] = field(default_factory=dict[str, _Working])

    def place_order(self, order: Order, decided_at: datetime | None = None) -> Fill | None:
        if order.client_id in self._book:
            return None
        self.orders[order.client_id] = order
        entry = _Working(order)
        self._book[order.client_id] = entry
        price = self.prices.get(order.ticker)
        if price is None or not self._crosses(order, price):
            return None
        entry.state, entry.filled, entry.price = "filled", order.quantity, price
        return Fill(
            order_client_id=order.client_id,
            ticker=order.ticker,
            quantity=order.quantity,
            price=price,
            fee=0.0,
            filled_at=datetime.now(UTC),
            side=order.side,
        )

    @staticmethod
    def _crosses(order: Order, price: float) -> bool:
        if order.order_type == "market" or order.limit_price is None:
            return True
        if order.side == "buy":
            return order.limit_price >= price
        return order.limit_price <= price

    def get_order_state(self, client_id: str) -> BrokerOrderState | None:
        entry = self._book.get(client_id)
        if entry is None:
            return None
        status = {"accepted": "pending", "filled": "filled", "cancelled": "cancelled"}[entry.state]
        return BrokerOrderState(
            client_id=client_id,
            broker_order_id=f"sim-{client_id}",
            ticker=entry.order.ticker,
            side=entry.order.side,
            status=status,  # type: ignore[arg-type]
            quantity=entry.order.quantity,
            filled_quantity=entry.filled,
            avg_fill_price=entry.price,
            state=entry.state,  # type: ignore[arg-type]
        )

    def cancel_order(self, client_id: str) -> bool:
        entry = self._book.get(client_id)
        if entry is None or entry.state != "accepted":
            return False
        entry.state = "cancelled"
        return True

    def cancel_all(self) -> int:
        working = self.working()
        for client_id in working:
            self._book[client_id].state = "cancelled"
        return len(working)

    def working(self) -> list[str]:
        return [cid for cid, e in self._book.items() if e.state == "accepted"]


# ---- the drill -------------------------------------------------------------------------


def broker_label(broker: object) -> str:
    kind = getattr(broker, "broker_kind", None)
    if isinstance(kind, str) and kind:
        return kind
    return type(broker).__name__.removesuffix("Broker").lower() or "broker"


def _open_portfolios(state: SqliteState) -> list[str]:
    rows = state.sql("SELECT id FROM portfolios WHERE status != 'archived' ORDER BY id")
    return [r["id"] for r in rows]


def engage_global_kill(broker: object, *, actor: str = DRILL_ACTOR) -> EngageKill:
    """The global kill switch in stop-all mode: the ``kill`` halt, then the
    working orders of every open portfolio cancelled at ``broker``."""

    def engage(state: SqliteState) -> int:
        halt, _ = trip_halt(
            state, "kill", reason=DRILL_REASON, actor=actor, scope="global", halt="all"
        )
        for portfolio_id in _open_portfolios(state):
            cancel_working_orders(broker, state, portfolio_id=portfolio_id)
        return halt.id

    return engage


def _refuse(state: SqliteState, broker: object) -> None:
    if getattr(broker, "mode", None) == "live":
        raise DrillRefusedError("the drill never runs against a live (real money) broker")
    missing = [
        name
        for name, ok in (
            ("place_order", callable(getattr(broker, "place_order", None))),
            ("cancel", isinstance(broker, OrderCanceller)),
            ("order_state", isinstance(broker, OrderStateSource)),
        )
        if not ok
    ]
    if missing:
        raise DrillRefusedError(f"the broker lacks {', '.join(missing)}")
    if list_halts(state):
        raise DrillRefusedError("a halt is in force here: the drill runs on a scratch state")


def _record_pending(state: SqliteState, order: Order, portfolio_id: str) -> None:
    from stonks.production.tick import _record_order

    _record_order(state, order, "pending", portfolio_id=portfolio_id)


def run_kill_switch_drill(
    state: SqliteState,
    broker: object,
    *,
    portfolio_id: str = DEFAULT_PORTFOLIO_ID,
    ticker: str = "AAPL.US",
    reference_price: float = 100.0,
    engage: EngageKill | None = None,
    cancel_timeout: float = 10.0,
    poll_seconds: float = 0.25,
    as_of: date | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> DrillReport:
    """Run the drill on ``state`` (a scratch DB) through ``broker``.
    Raises :class:`DrillRefusedError` before touching anything when the
    drill must not run; otherwise returns a report, passed or not."""
    if reference_price <= 0:
        raise ValueError("reference_price must be positive")
    _refuse(state, broker)
    engage = engage or engage_global_kill(broker)
    as_of = as_of or datetime.now(UTC).date()
    started = datetime.now(UTC)
    client_id = f"drill-{uuid4().hex[:12]}"
    limit = round(reference_price * FAR_FROM_MARKET, 2)
    order = Order(
        client_id=client_id,
        ticker=ticker,
        side="buy",
        quantity=1,
        order_type="limit",
        limit_price=limit,
        portfolio_id=portfolio_id,
        decision_price=reference_price,
        time_in_force="day",
        position_effect="open",
    )
    steps: list[DrillStep] = []
    halt_id: int | None = None
    cancel_seconds: float | None = None
    broker_state: Any = broker

    def step(name: str, fn: Callable[[], tuple[bool, str]]) -> bool:
        t0 = clock()
        try:
            ok, detail = fn()
        except Exception as exc:
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        steps.append(DrillStep(name, ok, detail, round((clock() - t0) * 1000.0, 1)))
        _log.info("drill.step", step=name, ok=ok, detail=detail)
        return ok

    def place() -> tuple[bool, str]:
        _record_pending(state, order, portfolio_id)
        fill = broker_state.place_order(order)
        reconcile_order(broker_state, state, client_id, reject_unknown=False)
        current = broker_state.get_order_state(client_id)
        if fill is not None or current is None or current.status != "pending":
            seen = "not found" if current is None else current.state or current.status
            return False, f"the drill order is not working at the broker ({seen})"
        return True, f"buy 1 {ticker} limit {limit:g} working ({current.state or 'pending'})"

    engaged_at = 0.0

    def kill() -> tuple[bool, str]:
        nonlocal halt_id, engaged_at
        engaged_at = clock()
        halt_id = engage(state)
        halt = get_halt(state, halt_id)
        ok = halt.kind == "kill" and halt.scope == "global" and halt.halt == "all"
        return ok, f"halt #{halt.id} ({halt.kind}, {halt.scope}, {halt.halt})"

    def blocked() -> tuple[bool, str]:
        sent_before = len(getattr(broker_state, "orders", {}) or {})
        verdict = run_gates(
            GateContext(
                state=state, as_of=as_of, portfolio_id=portfolio_id, owner_id=None, dry_run=True
            )
        )
        if verdict is None:
            return False, "no gate stops new orders"
        sent_after = len(getattr(broker_state, "orders", {}) or {})
        ok = verdict.halt == "all" and "kill" in verdict.reason and sent_after == sent_before
        return ok, f"{verdict.gate}: {verdict.halt} ({verdict.reason})"

    def cancelled() -> tuple[bool, str]:
        nonlocal cancel_seconds
        deadline = engaged_at + cancel_timeout
        current = broker_state.get_order_state(client_id)
        while current is not None and current.status != "cancelled" and clock() < deadline:
            sleep(poll_seconds)
            current = broker_state.get_order_state(client_id)
        if current is None or current.status != "cancelled":
            seen = "not found" if current is None else current.state or current.status
            return False, f"not cancelled within {cancel_timeout:g}s (broker: {seen})"
        cancel_seconds = round(clock() - engaged_at, 3)
        reconcile_order(broker_state, state, client_id, reject_unknown=False)
        row = state.sql("SELECT status FROM orders WHERE client_id = ?", [client_id])
        ledger = row[0]["status"] if row else "missing"
        return ledger == "cancelled", f"cancelled in {cancel_seconds:g}s, ledger {ledger}"

    def global_cancel() -> tuple[bool, str]:
        if not isinstance(broker_state, GlobalCanceller):
            return True, "the broker has no global cancel (skipped)"
        count = broker_state.cancel_all()
        return True, f"global cancel sent ({count} open before)"

    def resume() -> tuple[bool, str]:
        nonlocal halt_id
        if halt_id is None:
            return False, "no halt to clear"
        clear_halt(state, halt_id, actor=DRILL_ACTOR, reason="drill finished")
        return True, f"halt #{halt_id} cleared (a real resume needs RESUME TRADING)"

    try:
        if step("place_working_order", place) and step("engage_kill", kill):
            step("new_orders_blocked", blocked)
            step("working_order_cancelled", cancelled)
            step("global_cancel", global_cancel)
            step("resume", resume)
    finally:
        _cleanup(state, broker_state, client_id, halt_id)
    report = DrillReport(
        broker=broker_label(broker),
        portfolio_id=portfolio_id,
        client_id=client_id,
        started_at=started,
        finished_at=datetime.now(UTC),
        steps=tuple(steps),
        halt_id=halt_id,
        cancel_seconds=cancel_seconds,
    )
    _log.warning("drill.done", passed=report.passed, broker=report.broker, halt_id=halt_id)
    return report


def _cleanup(state: SqliteState, broker: Any, client_id: str, halt_id: int | None) -> None:
    """Cancel the drill order if it still works and clear the drill's halt
    if it is still open. Never raises."""
    try:
        current = broker.get_order_state(client_id)
        if current is not None and current.status in ("pending", "partially_filled"):
            broker.cancel_order(client_id)
    except Exception as exc:
        _log.error("drill.cleanup_cancel_failed", client_id=client_id, error=str(exc))
    if halt_id is None:
        return
    try:
        if not get_halt(state, halt_id).cleared:
            clear_halt(state, halt_id, actor=DRILL_ACTOR, reason="drill cleanup")
    except Exception as exc:
        _log.error("drill.cleanup_halt_failed", halt_id=halt_id, error=str(exc))
