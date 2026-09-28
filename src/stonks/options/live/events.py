"""Option assignments, exercises and expiries into the ledger (roadmap 17.8).

A broker reports what happened to an option position outside an order: a
short was assigned, a long exercised, or either expired. Each becomes an
:class:`OptionEvent` and is booked once (``option_events``, unique per
portfolio and broker event id):

- the option leg leaves the book at price 0 (an order and a fill closing
  the contracts);
- physical settlement delivers ``quantity x multiplier`` shares of the
  underlying at the strike (an order and a fill). A short call assigned
  sells shares, a short put assigned buys them, a long call exercised buys
  and a long put exercised sells;
- an expiry or a cash settlement moves no shares.

Rows are booked through :func:`stonks.execution.reconcile.book_executions`
with the event id as the execution id, so a second run books nothing.
The owner hears of every assignment and exercise (high urgency).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time
from typing import Literal

from stonks.core.clock import SYSTEM_CLOCK, Clock, iso_now
from stonks.core.options import OptionContract, parse_contract_id
from stonks.core.types import Order
from stonks.execution.brokers.base import Execution
from stonks.logging import get_logger
from stonks.store.state import SqliteState

_log = get_logger("stonks.options.live.events")

OptionEventKind = Literal["assignment", "exercise", "expiry"]
TABLE = "option_events"


@dataclass(frozen=True)
class OptionEvent:
    """One broker event. ``quantity`` is the signed position it removed
    (negative: a short). ``settlement`` follows the contract."""

    event_id: str
    kind: OptionEventKind
    contract_id: str
    quantity: float
    occurred_on: date
    source: str = "broker"

    @property
    def contract(self) -> OptionContract:
        return parse_contract_id(self.contract_id)

    def shares(self) -> float:
        """Signed shares delivered into the book (0: none)."""
        c = self.contract
        if self.kind == "expiry" or c.settlement == "cash":
            return 0.0
        contracts = abs(self.quantity)
        # a short call assigned or a long put exercised sells shares
        sells = (c.is_call and self.quantity < 0) or (not c.is_call and self.quantity > 0)
        return (-1.0 if sells else 1.0) * contracts * c.multiplier


def events_enabled(state: SqliteState) -> bool:
    rows = state.sql("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", [TABLE])
    return bool(rows)


def _cid(portfolio_id: str, event_id: str, leg: str) -> str:
    digest = hashlib.sha256(f"{portfolio_id}|{event_id}".encode()).hexdigest()[:16]
    return f"eae-{digest}:{leg}"


@dataclass(frozen=True)
class BookedEvent:
    event: OptionEvent
    shares: float
    client_ids: tuple[str, ...]


def book_events(
    state: SqliteState,
    portfolio_id: str,
    events: Sequence[OptionEvent],
    *,
    clock: Clock = SYSTEM_CLOCK,
) -> list[BookedEvent]:
    """Book the events not booked before. Returns the new ones."""
    if not events_enabled(state):
        return []
    from stonks.execution.reconcile import book_executions
    from stonks.production.tick import _record_order

    booked: list[BookedEvent] = []
    for event in events:
        known = state.sql(
            f"SELECT 1 FROM {TABLE} WHERE portfolio_id = ? AND event_id = ?",
            [portfolio_id, event.event_id],
        )
        if known:
            continue
        try:
            contract = event.contract
        except ValueError:
            _log.warning("options.event.unknown_contract", event_id=event.event_id)
            continue
        at = datetime.combine(event.occurred_on, time(21, 0), tzinfo=UTC)
        shares = event.shares()
        legs: list[tuple[Order, Execution]] = []
        close_side = "buy" if event.quantity < 0 else "sell"
        option_cid = _cid(portfolio_id, event.event_id, "opt")
        legs.append(
            (
                Order(
                    client_id=option_cid,
                    ticker=event.contract_id,
                    side=close_side,
                    quantity=abs(event.quantity),
                    # no order reached the broker: the row only carries the event
                    order_type="market",
                    position_effect="close",
                    decision_context={"option_event": event.kind, "event_id": event.event_id},
                ),
                Execution(
                    broker_exec_id=f"{event.event_id}:opt",
                    client_id=option_cid,
                    ticker=event.contract_id,
                    side=close_side,
                    quantity=abs(event.quantity),
                    price=0.0,
                    executed_at=at,
                ),
            )
        )
        if shares:
            share_cid = _cid(portfolio_id, event.event_id, "stk")
            side = "buy" if shares > 0 else "sell"
            legs.append(
                (
                    Order(
                        client_id=share_cid,
                        ticker=contract.underlying,
                        side=side,
                        quantity=abs(shares),
                        order_type="limit",
                        limit_price=contract.strike,
                        decision_context={
                            "option_event": event.kind,
                            "event_id": event.event_id,
                            "contract_id": event.contract_id,
                        },
                    ),
                    Execution(
                        broker_exec_id=f"{event.event_id}:stk",
                        client_id=share_cid,
                        ticker=contract.underlying,
                        side=side,
                        quantity=abs(shares),
                        price=contract.strike,
                        executed_at=at,
                    ),
                )
            )
        with state.transaction():
            for order, _ in legs:
                _record_order(state, order, status="pending", portfolio_id=portfolio_id)
            state.execute(
                f"INSERT INTO {TABLE} (portfolio_id, event_id, kind, contract_id, underlying,"
                " quantity, shares, strike, occurred_on, source, booked_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    portfolio_id,
                    event.event_id,
                    event.kind,
                    event.contract_id,
                    contract.underlying,
                    event.quantity,
                    shares,
                    contract.strike,
                    event.occurred_on.isoformat(),
                    event.source,
                    iso_now(clock),
                ],
            )
        book_executions(state, [e for _, e in legs], portfolio_id=portfolio_id, clock=clock)
        _log.info(
            "options.event.booked",
            portfolio_id=portfolio_id,
            kind=event.kind,
            contract_id=event.contract_id,
            shares=shares,
        )
        booked.append(
            BookedEvent(event=event, shares=shares, client_ids=tuple(o.client_id for o, _ in legs))
        )
    return booked


def with_source(events: Sequence[OptionEvent], source: str) -> list[OptionEvent]:
    return [replace(e, source=source) for e in events]
