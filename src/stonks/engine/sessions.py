"""Session rules for the intraday engine (roadmap 21.2.4).

When may the engine trade a ticker at an instant? This module answers with
pure functions, so the event driver, the intraday backtest and replay all
ask the same question the same way. It imports nothing from the driver.

- Regular hours only. Sessions come from ``scheduling.calendar`` (the
  ``exchange_calendars`` seam), so holidays, early closes and DST are the
  calendar's job. Pre-market and after-hours are ``closed``.
- No new entries in the first ``entry_delay_minutes`` after the open and
  the last ``entry_cutoff_minutes`` before the close. Exits stay allowed.
- A book with ``flatten_at_close`` closes every position in the last
  ``flatten_minutes`` (:func:`flatten_orders`).
- A trading halt (an LULD pause or an exchange halt, fed in as data through
  :class:`HaltTable`) blocks every order in that ticker, exits included:
  the exchange would not take them.
- An early close moves every edge with it, because the edges are measured
  from the session's own close.
- A 24/7 calendar has no edges: its day boundaries are not a real open or
  close.

Every rule here only removes orders or closes positions, never adds risk
(P28). The phase order, first match wins: closed, halted, flatten,
closing, opening, regular.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks.core.types import Order
from stonks.execution.orders import classify_all, make_client_id, side_token
from stonks.scheduling.calendar import (
    AlwaysOpenCalendar,
    CalendarRangeError,
    MarketCalendar,
    Session,
    UnknownCalendarError,
    calendar_for_ticker,
    ensure_utc,
)

SessionPhase = Literal["closed", "halted", "flatten", "closing", "opening", "regular"]
HaltReason = Literal["luld", "exchange", "regulatory", "news", "stale", "other"]
_HALT_REASONS = frozenset(get_args(HaltReason))

#: The strategy id flatten orders carry in their client id.
FLATTEN_STRATEGY_ID = "flatten"


class SessionRules(BaseModel):
    """Per-book session settings. Zero minutes turns an edge off."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: No new entries this many minutes after the open.
    entry_delay_minutes: int = Field(default=5, ge=0)
    #: No new entries this many minutes before the close.
    entry_cutoff_minutes: int = Field(default=10, ge=0)
    #: Close every position in the last ``flatten_minutes``.
    flatten_at_close: bool = False
    flatten_minutes: int = Field(default=5, ge=0)

    @model_validator(mode="after")
    def _flatten_needs_a_window(self) -> SessionRules:
        if self.flatten_at_close and self.flatten_minutes == 0:
            raise ValueError("flatten_at_close needs flatten_minutes > 0")
        return self


@dataclass(frozen=True)
class TradingHalt:
    """One trading halt of one ticker, from ``start`` until ``end``
    (exclusive). ``end`` is ``None`` while the halt is still on."""

    ticker: str
    start: datetime
    end: datetime | None
    reason: HaltReason

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", ensure_utc(self.start))
        if self.end is not None:
            end = ensure_utc(self.end)
            if end < self.start:
                raise ValueError(f"halt end {end} is before its start {self.start}")
            object.__setattr__(self, "end", end)
        if self.reason not in _HALT_REASONS:
            raise ValueError(f"unknown halt reason {self.reason!r}")

    def covers(self, at: datetime) -> bool:
        return self.start <= at and (self.end is None or at < self.end)


class HaltTable:
    """Trading halts by ticker, as data. An immutable value: :meth:`add`
    and :meth:`resume` return a new table, so a replay can keep one table
    per instant without copies going stale."""

    def __init__(self, halts: Iterable[TradingHalt] = ()) -> None:
        by_ticker: dict[str, tuple[TradingHalt, ...]] = {}
        for halt in halts:
            by_ticker[halt.ticker] = (*by_ticker.get(halt.ticker, ()), halt)
        self._by_ticker = by_ticker

    def __len__(self) -> int:
        return sum(len(v) for v in self._by_ticker.values())

    def __iter__(self) -> Iterator[TradingHalt]:
        for halts in self._by_ticker.values():
            yield from halts

    def active(self, ticker: str, at: datetime) -> TradingHalt | None:
        """The halt on ``ticker`` at ``at``, or ``None``."""
        for halt in self._by_ticker.get(ticker, ()):
            if halt.covers(at):
                return halt
        return None

    def add(self, halt: TradingHalt) -> HaltTable:
        return HaltTable([*self, halt])

    def resume(self, ticker: str, at: datetime) -> HaltTable:
        """Close every open-ended halt on ``ticker`` at ``at``."""
        at = ensure_utc(at)
        return HaltTable(
            replace(h, end=max(at, h.start)) if h.ticker == ticker and h.end is None else h
            for h in self
        )


NO_HALTS = HaltTable()


@dataclass(frozen=True)
class SessionState:
    """What the engine may do in ``ticker`` at ``at``."""

    ticker: str
    at: datetime
    phase: SessionPhase
    #: The session ``at`` falls in, or ``None`` when closed.
    session: Session | None
    halt: TradingHalt | None = None

    @property
    def halted(self) -> bool:
        return self.halt is not None

    @property
    def can_open(self) -> bool:
        """New entries (orders that open or grow a position)."""
        return self.phase == "regular"

    @property
    def can_close(self) -> bool:
        """Exits (orders that reduce a position)."""
        return self.phase in ("regular", "opening", "closing", "flatten")

    @property
    def must_flatten(self) -> bool:
        return self.phase == "flatten"

    @property
    def reason(self) -> str | None:
        """Why entries are blocked, or ``None`` in regular hours."""
        return None if self.phase == "regular" else self.phase

    @property
    def minutes_to_close(self) -> float | None:
        if self.session is None:
            return None
        return (self.session.close - self.at).total_seconds() / 60


def session_at(calendar: MarketCalendar, at: datetime) -> Session | None:
    """The session of ``calendar`` that contains ``at``, if any."""
    at = ensure_utc(at)
    day = at.date()
    # A session's local date can differ from its UTC date by one day.
    for d in (day - timedelta(days=1), day, day + timedelta(days=1)):
        s = calendar.session(d)
        if s is not None and s.open <= at < s.close:
            return s
    return None


def session_state(
    at: datetime,
    ticker: str,
    *,
    calendar: MarketCalendar,
    rules: SessionRules,
    halts: HaltTable = NO_HALTS,
    session: Session | None = None,
) -> SessionState:
    """The session state of ``ticker`` at ``at``. Pure: same inputs, same
    answer. ``session`` skips the calendar lookup when the caller already
    knows the session ``at`` falls in."""
    at = ensure_utc(at)
    if session is None or not session.open <= at < session.close:
        session = session_at(calendar, at)
    return _phase_state(at, ticker, session, rules, halts, _is_continuous(calendar))


def _phase_state(
    at: datetime,
    ticker: str,
    session: Session | None,
    rules: SessionRules,
    halts: HaltTable,
    continuous: bool,
) -> SessionState:
    if session is None:
        return SessionState(ticker, at, "closed", None)
    halt = halts.active(ticker, at)
    if halt is not None:
        return SessionState(ticker, at, "halted", session, halt)
    if continuous:
        return SessionState(ticker, at, "regular", session)
    to_close = session.close - at
    if rules.flatten_at_close and to_close <= timedelta(minutes=rules.flatten_minutes):
        return SessionState(ticker, at, "flatten", session)
    if to_close <= timedelta(minutes=rules.entry_cutoff_minutes):
        return SessionState(ticker, at, "closing", session)
    if at - session.open < timedelta(minutes=rules.entry_delay_minutes):
        return SessionState(ticker, at, "opening", session)
    return SessionState(ticker, at, "regular", session)


def _is_continuous(calendar: MarketCalendar) -> bool:
    return isinstance(calendar, AlwaysOpenCalendar)


class SessionRulebook:
    """:func:`session_state` with the calendar picked per ticker (by asset
    class, else by exchange suffix) and sessions cached per day. A ticker
    with no known calendar is ``closed``: the engine never trades what it
    cannot place in a session."""

    def __init__(self, rules: SessionRules, *, halts: HaltTable = NO_HALTS) -> None:
        self.rules = rules
        self.halts = halts

    def with_halts(self, halts: HaltTable) -> SessionRulebook:
        return SessionRulebook(self.rules, halts=halts)

    def state(self, at: datetime, ticker: str, *, asset_class: str | None = None) -> SessionState:
        at = ensure_utc(at)
        try:
            calendar = calendar_for_ticker(ticker, asset_class)
            session = _cached_session_at(calendar, at.date(), at)
        except (UnknownCalendarError, CalendarRangeError):
            return SessionState(ticker, at, "closed", None)
        return _phase_state(at, ticker, session, self.rules, self.halts, _is_continuous(calendar))


def _cached_session_at(calendar: MarketCalendar, day: date, at: datetime) -> Session | None:
    for d in (day - timedelta(days=1), day, day + timedelta(days=1)):
        s = _cached_session(calendar, d)
        if s is not None and s.open <= at < s.close:
            return s
    return None


@lru_cache(maxsize=4096)
def _cached_session(calendar: MarketCalendar, day: date) -> Session | None:
    return calendar.session(day)


# ---- orders ---------------------------------------------------------------------


@dataclass(frozen=True)
class DroppedOrder:
    order: Order
    reason: str


def gate_orders(
    orders: Sequence[Order],
    positions: Mapping[str, float],
    state_for: Callable[[str], SessionState],
) -> tuple[list[Order], list[DroppedOrder]]:
    """Split ``orders`` into the ones the session allows and the ones it
    drops. Orders are classified against ``positions`` first
    (``execution.orders.classify_all``), so an order that crosses zero is
    split and only its closing leg survives an entry block. A halted or
    closed ticker gets no orders at all."""
    kept: list[Order] = []
    dropped: list[DroppedOrder] = []
    states: dict[str, SessionState] = {}
    for order in classify_all(orders, positions):
        state = states.get(order.ticker)
        if state is None:
            state = states[order.ticker] = state_for(order.ticker)
        allowed = state.can_close if order.position_effect == "close" else state.can_open
        if allowed:
            kept.append(order)
        else:
            dropped.append(DroppedOrder(order, state.reason or state.phase))
    return kept, dropped


def flatten_orders(
    positions: Mapping[str, float],
    state_for: Callable[[str], SessionState],
    *,
    portfolio_id: str | None = None,
    prices: Mapping[str, float] | None = None,
    collar_bps: float = 25.0,
) -> list[Order]:
    """Closing day orders for every position whose ticker is in the flatten
    window. With a known price the order is a marketable limit collared
    ``collar_bps`` through it, else a market order. Client ids are stable
    per session day, so asking again on the next bar does not double up."""
    out: list[Order] = []
    for ticker, held in positions.items():
        if held == 0:
            continue
        state = state_for(ticker)
        if not state.must_flatten or state.session is None:
            continue
        side = "sell" if held > 0 else "buy"
        price = prices.get(ticker) if prices else None
        limit = None
        if price is not None and price > 0:
            sign = -1.0 if side == "sell" else 1.0
            limit = price * (1 + sign * collar_bps / 10_000)
        draft = Order(
            client_id="",
            ticker=ticker,
            side=side,
            quantity=abs(held),
            order_type="limit" if limit is not None else "market",
            limit_price=limit,
            strategy_id=FLATTEN_STRATEGY_ID,
            portfolio_id=portfolio_id,
            decided_at=state.at,
            position_effect="close",
            time_in_force="day",
        )
        client_id = make_client_id(
            as_of=state.session.date,
            strategy_id=FLATTEN_STRATEGY_ID,
            ticker=ticker,
            side=side_token(draft),
            portfolio_id=portfolio_id,
        )
        out.append(replace(draft, client_id=client_id))
    return out
