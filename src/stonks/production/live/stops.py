"""Broker-side protective stops (roadmap 19.10, design
``docs/design/live-trading.md`` section 4).

Optional and off by default (``[production.risk.rules.protective_stops]``,
per portfolio and per strategy through the tighten-only overrides). When on,
every position Stonks opened carries one good till cancelled stop at the
broker, so it stays protected while Stonks or the gateway is down:

- **Placement.** After an entry fills, the next sync places a ``stop``
  order that closes the whole position: a sell below a long, a buy above a
  short, ``atr_multiple`` ATRs from the entry price (``fallback_pct`` of it
  without an ATR). A stop never sits beyond the market: when the price has
  already passed the entry-based level, it is measured from the close.
- **Ids.** The stop's client id is the entry's plus ``:stop`` (then
  ``:stop:2``, ...), so it goes through the order state machine,
  reconciliation and every idempotency check like any order. Its OCA group
  is one per position entry (:func:`oca_group_for`). The tick's exits of
  that position join the group (:func:`tag_exits`): at the broker a fill of
  either shrinks the other, so the position is never sold twice.
- **Resizing.** When the position changes, the stop is cancelled and a new
  one placed for the new size. A smaller position keeps the stop price, a
  grown one (a new entry) is priced from the new average cost.
- **Cancelling.** A closed position cancels its stop, and so does turning
  stops off.
- **Attribution.** Only Stonks' own positions get stops: the quantity is
  the book's view (the owner's manual holdings in a shared account are
  never protected, BE-02), and the entry, cost and strategy come from the
  book's own fills. A stop fill is a normal closing fill of the strategy
  that held the lots (``decision_context.trigger == "stop"``), and the
  ``stop_cooldown`` and ``stop_guard`` protections count it as a stop-out.

At a real broker :func:`send_stop_plan` sends the plan through the broker
(cancel first, then place). A simulated book keeps its stops in the ledger:
:func:`record_paper_plan` writes them as working orders and
:func:`sweep_paper_stops` fills them from the bar ranges since they were
placed, through the simulated broker's resting stops.

Stops only reduce risk. A stop fills at the market after a gap, so it
limits how long a loss can run while nobody watches, not the size of an
overnight gap.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from stonks.core.clock import SYSTEM_CLOCK, Clock
from stonks.core.timeutil import day_start
from stonks.core.types import Fill, Order, OrderSide, Portfolio
from stonks.execution.brokers.base import OrderCanceller, OrderRejectedError, OrderStateSource
from stonks.execution.order_state import (
    TERMINAL,
    can_transition,
    current_state,
    mark_unknown,
    write_state,
)
from stonks.execution.reconcile import NON_TERMINAL_STATUSES, reconcile_order
from stonks.logging import get_logger
from stonks.production.ledger import ledger_columns
from stonks.production.rules._stop_settings import ProtectiveStopSettings

if TYPE_CHECKING:
    from stonks.backtest.simulated_broker import SimulatedBroker
    from stonks.store.lake import DuckDBLake
    from stonks.store.state import SqliteState

_log = get_logger("stonks.production.live.stops")

#: ``decision_context.trigger`` of a protective stop.
STOP_TRIGGER = "stop"
_EPS = 1e-9
_OCA_PREFIX = "stk-oca-"

#: Why a working stop is cancelled, in the words the ledger and console show.
CANCEL_WORDS: Mapping[str, str] = {
    "position closed": "the position closed",
    "resized": "replaced by a stop for the new position size",
    "stops turned off": "protective stops were turned off",
    "duplicate": "another stop already protects the position",
}

#: The settings of one strategy (``None``: the whole book).
SettingsFor = Callable[[str | None], ProtectiveStopSettings]


# ---- the values --------------------------------------------------------------------


@dataclass(frozen=True)
class Holding:
    """A position Stonks owns and may protect."""

    ticker: str
    #: Signed: shorts are negative.
    quantity: float
    #: The order that opened or last grew the position.
    entry_client_id: str
    #: The average cost of the position.
    entry_price: float
    strategy_id: str | None = None


@dataclass(frozen=True)
class WorkingStop:
    """A protective stop in the ledger that is not finished yet."""

    client_id: str
    ticker: str
    side: OrderSide
    quantity: float
    stop_price: float
    strategy_id: str | None
    oca_group: str | None
    #: The entry it protects (``None`` on a row with no decision context).
    entry_client_id: str | None
    #: The fine order state.
    state: str


@dataclass(frozen=True)
class StopCancel:
    stop: WorkingStop
    #: A key of :data:`CANCEL_WORDS`.
    reason: str


@dataclass(frozen=True)
class StopPlan:
    place: tuple[Order, ...] = ()
    cancel: tuple[StopCancel, ...] = ()
    keep: tuple[WorkingStop, ...] = ()
    #: ``(ticker, reason)`` of positions that get no stop.
    unprotected: tuple[tuple[str, str], ...] = ()

    @property
    def empty(self) -> bool:
        return not self.place and not self.cancel


@dataclass(frozen=True)
class LedgerFill:
    """One fill of the book's own orders, oldest first."""

    client_id: str
    strategy_id: str | None
    ticker: str
    side: OrderSide
    quantity: float
    price: float


@dataclass(frozen=True)
class StopSync:
    """What a sync did."""

    placed: tuple[str, ...] = ()
    cancelled: tuple[str, ...] = ()
    kept: int = 0
    #: Client ids whose send or cancel failed (left for reconciliation).
    failed: tuple[str, ...] = ()
    unprotected: tuple[tuple[str, str], ...] = ()
    #: Stops that filled (a simulated book's sweep).
    filled: tuple[str, ...] = field(default=())

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"placed": len(self.placed), "cancelled": len(self.cancelled),
                               "kept": self.kept}  # fmt: skip
        if self.failed:
            out["failed"] = list(self.failed)
        if self.unprotected:
            out["unprotected"] = [f"{t}: {r}" for t, r in self.unprotected]
        if self.filled:
            out["filled"] = list(self.filled)
        return out


# ---- the pure part -----------------------------------------------------------------


def stop_client_id(entry_client_id: str, taken: AbstractSet[str]) -> str:
    """``<entry>:stop``, then ``<entry>:stop:2`` and so on past the taken ids."""
    base = f"{entry_client_id}:stop"
    if base not in taken:
        return base
    n = 2
    while f"{base}:{n}" in taken:
        n += 1
    return f"{base}:{n}"


def oca_group_for(portfolio_id: str, ticker: str, entry_client_id: str) -> str:
    """The OCA group of one position entry: stable, short enough for any
    broker, and new for each entry (a broker may refuse to reuse a group
    whose orders already filled)."""
    raw = f"{portfolio_id}|{ticker}|{entry_client_id}".encode()
    return _OCA_PREFIX + hashlib.sha256(raw).hexdigest()[:20]


def _finite(value: float | None) -> float | None:
    return float(value) if value is not None and math.isfinite(value) and value > 0 else None


def stop_price_for(
    holding: Holding,
    settings: ProtectiveStopSettings,
    *,
    atr: float | None,
    close: float | None,
) -> float | None:
    """Where the stop of ``holding`` sits, or ``None`` when there is no room
    for one (the distance reaches zero)."""
    atr = _finite(atr)
    close = _finite(close)
    long = holding.quantity > 0

    def level(reference: float) -> float:
        dist = settings.atr_multiple * atr if atr is not None else reference * settings.fallback_pct
        return reference - dist if long else reference + dist

    price = level(holding.entry_price)
    if close is not None and (price >= close if long else price <= close):
        price = level(close)  # the market already passed it: protect from here
    if price <= _EPS:
        return None
    return round(price, 6)


def plan_stops(
    holdings: Mapping[str, Holding],
    working: Sequence[WorkingStop],
    *,
    settings_for: SettingsFor,
    atr: Mapping[str, float],
    closes: Mapping[str, float],
    portfolio_id: str,
    as_of: date,
    tick_id: str | None = None,
    taken: AbstractSet[str] = frozenset(),
) -> StopPlan:
    """What to place, keep and cancel so every protected holding carries
    exactly one stop of its size (see the module doc)."""
    by_ticker: dict[str, list[WorkingStop]] = {}
    for stop in working:
        by_ticker.setdefault(stop.ticker, []).append(stop)
    taken_ids = set(taken) | {s.client_id for s in working}
    place: list[Order] = []
    cancel: list[StopCancel] = []
    keep: list[WorkingStop] = []
    unprotected: list[tuple[str, str]] = []

    for ticker in sorted(set(by_ticker) | set(holdings)):
        stops = by_ticker.get(ticker, [])
        holding = holdings.get(ticker)
        settings = settings_for(holding.strategy_id) if holding is not None else None
        if holding is None or settings is None or not settings.enabled:
            reason = "position closed" if holding is None else "stops turned off"
            cancel.extend(StopCancel(s, reason) for s in stops)
            continue
        side: OrderSide = "sell" if holding.quantity > 0 else "buy"
        size = abs(holding.quantity)
        match = next(
            (
                s
                for s in stops
                if s.side == side
                and abs(s.quantity - size) <= _EPS * max(1.0, size)
                and s.entry_client_id == holding.entry_client_id
            ),
            None,
        )
        if match is not None:
            keep.append(match)
            cancel.extend(StopCancel(s, "duplicate") for s in stops if s is not match)
            continue
        cancel.extend(StopCancel(s, "resized") for s in stops)
        same_entry = next(
            (s for s in stops if s.side == side and s.entry_client_id == holding.entry_client_id),
            None,
        )
        tick_atr = atr.get(ticker)
        price = (
            same_entry.stop_price
            if same_entry is not None
            else stop_price_for(holding, settings, atr=tick_atr, close=closes.get(ticker))
        )
        if price is None:
            where = "below" if side == "sell" else "above"
            unprotected.append((ticker, f"no room for a stop {where} the price"))
            continue
        client_id = stop_client_id(holding.entry_client_id, taken_ids)
        taken_ids.add(client_id)
        context: dict[str, Any] = {
            "trigger": STOP_TRIGGER,
            "strategy_id": holding.strategy_id,
            "entry_client_id": holding.entry_client_id,
            "entry_price": holding.entry_price,
            "atr": _finite(tick_atr),
            "atr_multiple": settings.atr_multiple,
        }
        if stops:
            context["replaces"] = stops[0].client_id
        place.append(
            Order(
                client_id=client_id,
                ticker=ticker,
                side=side,
                quantity=size,
                order_type="stop",
                stop_price=price,
                time_in_force="gtc",
                position_effect="close",
                strategy_id=holding.strategy_id,
                tick_id=tick_id,
                portfolio_id=portfolio_id,
                oca_group=oca_group_for(portfolio_id, ticker, holding.entry_client_id),
                decision_price=_finite(closes.get(ticker)),
                decided_at=datetime.combine(as_of, time(), UTC),
                decision_context=context,
            )
        )
    return StopPlan(
        place=tuple(place),
        cancel=tuple(cancel),
        keep=tuple(keep),
        unprotected=tuple(unprotected),
    )


def holdings_from_fills(
    fills: Iterable[LedgerFill], positions: Mapping[str, float]
) -> dict[str, Holding]:
    """The holdings of ``positions`` (the book's own view) the fills explain:
    per ticker the average cost, and the order and strategy that opened or
    last grew it. A position the fills do not explain (another side, or no
    fill at all) is not the book's to protect."""
    held: dict[str, float] = {}
    cost: dict[str, float] = {}
    entry: dict[str, LedgerFill] = {}
    for f in fills:
        q = held.get(f.ticker, 0.0)
        signed = f.quantity if f.side == "buy" else -f.quantity
        after = q + signed
        if abs(q) <= _EPS or q * signed > 0:  # opens or grows
            cost[f.ticker] = cost.get(f.ticker, 0.0) + signed * f.price
            entry[f.ticker] = f
        elif abs(after) <= _EPS:  # closed
            cost.pop(f.ticker, None)
            entry.pop(f.ticker, None)
        elif after * q < 0:  # crossed zero: the rest opens the other side
            cost[f.ticker] = after * f.price
            entry[f.ticker] = f
        else:  # shrank
            cost[f.ticker] = cost.get(f.ticker, 0.0) / q * after
        held[f.ticker] = 0.0 if abs(after) <= _EPS else after
    out: dict[str, Holding] = {}
    for ticker in sorted(positions):
        qty = float(positions[ticker])
        q = held.get(ticker, 0.0)
        if abs(qty) <= _EPS or ticker not in entry or q * qty <= 0:
            continue
        first = entry[ticker]
        out[ticker] = Holding(
            ticker=ticker,
            quantity=qty,
            entry_client_id=first.client_id,
            entry_price=abs(cost[ticker] / q),
            strategy_id=first.strategy_id,
        )
    return out


def tag_exits(orders: Sequence[Order], working: Sequence[WorkingStop]) -> list[Order]:
    """Orders that shrink a position with a working stop (the stop's side,
    never a short sale) join the stop's OCA group, so a fill of either
    shrinks the other at the broker."""
    stops = {s.ticker: s for s in working if s.oca_group}
    out: list[Order] = []
    for order in orders:
        stop = stops.get(order.ticker)
        shrinks = stop is not None and order.side == stop.side and order.position_effect != "open"
        if stop is not None and shrinks and not order.oca_group:
            out.append(replace(order, oca_group=stop.oca_group))
        else:
            out.append(order)
    return out


# ---- reading the ledger and the lake ---------------------------------------------------


def stops_recorded(state: SqliteState) -> bool:
    """Whether the ledger has the protective stop columns (migration 034)."""
    return "protective" in ledger_columns(state, "orders")


def load_ledger_fills(state: SqliteState, portfolio_id: str) -> list[LedgerFill]:
    """The book's own fills (manual orders are the person's), oldest first."""
    cols = ledger_columns(state, "orders")
    manual = " AND o.origin <> 'manual'" if "origin" in cols else ""
    rows = state.sql(
        "SELECT f.order_client_id, o.strategy_id, f.ticker, o.side, f.quantity, f.price"
        " FROM fills f JOIN orders o ON o.client_id = f.order_client_id"
        f" WHERE f.portfolio_id = ?{manual} ORDER BY f.filled_at, f.id",
        [portfolio_id],
    )
    return [
        LedgerFill(
            client_id=r["order_client_id"],
            strategy_id=r["strategy_id"],
            ticker=r["ticker"],
            side=r["side"],
            quantity=float(r["quantity"]),
            price=float(r["price"]),
        )
        for r in rows
    ]


def load_working_stops(state: SqliteState, portfolio_id: str) -> list[WorkingStop]:
    """The book's protective stops that are not finished, oldest first."""
    if not stops_recorded(state):
        return []
    marks = ",".join("?" for _ in NON_TERMINAL_STATUSES)
    rows = state.sql(
        "SELECT client_id, ticker, side, quantity, stop_price, strategy_id, oca_group, state,"
        " status, decision_context_json FROM orders"
        f" WHERE portfolio_id = ? AND protective = 1 AND status IN ({marks})"
        " ORDER BY created_at, rowid",
        [portfolio_id, *NON_TERMINAL_STATUSES],
    )
    out: list[WorkingStop] = []
    for r in rows:
        context = _context(r["decision_context_json"])
        out.append(
            WorkingStop(
                client_id=r["client_id"],
                ticker=r["ticker"],
                side=r["side"],
                quantity=float(r["quantity"]),
                stop_price=float(r["stop_price"] or 0.0),
                strategy_id=r["strategy_id"],
                oca_group=r["oca_group"],
                entry_client_id=context.get("entry_client_id"),
                state=r["state"] or r["status"],
            )
        )
    return out


def stop_ids(state: SqliteState, portfolio_id: str) -> set[str]:
    """Every protective stop client id the book ever used."""
    if not stops_recorded(state):
        return set()
    rows = state.sql(
        "SELECT client_id FROM orders WHERE portfolio_id = ? AND protective = 1", [portfolio_id]
    )
    return {r["client_id"] for r in rows}


def _context(raw: object) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def load_atr(
    lake: DuckDBLake | None, tickers: Sequence[str], as_of: date, window: int
) -> tuple[dict[str, float], dict[str, float]]:
    """``(ATR, last close)`` per ticker from the traded (unadjusted) daily
    bars up to ``as_of``. A ticker with too few bars has no ATR."""
    from stonks.features.indicators import atr as wilder_atr

    if lake is None or not tickers:
        return {}, {}
    df = lake.sql(
        """
        SELECT ticker, timestamp, high, low, close FROM (
            SELECT ticker, timestamp, high, low, close,
                   row_number() OVER (PARTITION BY ticker ORDER BY timestamp DESC) AS rn
              FROM bars WHERE interval = '1d' AND ticker = ANY(?) AND timestamp < ?)
         WHERE rn <= ? ORDER BY ticker, timestamp
        """,
        [sorted(set(tickers)), day_start(as_of + timedelta(days=1)), window * 3 + 1],
    )
    atrs: dict[str, float] = {}
    closes: dict[str, float] = {}
    for ticker, group in df.groupby("ticker", sort=True):
        name = str(ticker)
        closes[name] = float(group["close"].iloc[-1])
        series = wilder_atr(group["high"], group["low"], group["close"], window)
        last = series.iloc[-1] if len(series) else float("nan")
        if last == last and last > 0:  # not NaN
            atrs[name] = float(last)
    return atrs, closes


def book_settings(policy: Any, overrides: Mapping[str, Any]) -> SettingsFor:
    """The protective stop settings of each strategy's slice (the book's
    policy for the whole book or a strategy without an override)."""
    from stonks.production.rules._common import settings_of

    def for_strategy(strategy_id: str | None) -> ProtectiveStopSettings:
        slice_policy = overrides.get(strategy_id) if strategy_id is not None else None
        found = settings_of(slice_policy if slice_policy is not None else policy,
                            "protective_stops")  # fmt: skip
        return found if isinstance(found, ProtectiveStopSettings) else ProtectiveStopSettings()

    return for_strategy


def any_enabled(policy: Any, overrides: Mapping[str, Any]) -> bool:
    """Whether the book or any strategy slice turns stops on."""
    settings = book_settings(policy, overrides)
    return settings(None).enabled or any(settings(sid).enabled for sid in overrides)


# ---- planning a book ---------------------------------------------------------------------


def plan_book(
    state: SqliteState,
    lake: DuckDBLake | None,
    *,
    portfolio_id: str,
    positions: Mapping[str, float],
    settings_for: SettingsFor,
    as_of: date,
    tick_id: str | None = None,
    extra_fills: Sequence[LedgerFill] = (),
    working: Sequence[WorkingStop] | None = None,
) -> StopPlan:
    """The stop plan of one book: its own ``positions`` (after this run's
    fills), its ledger fills plus ``extra_fills`` (a simulated run's fills,
    not recorded yet) and its working stops."""
    working = list(working) if working is not None else load_working_stops(state, portfolio_id)
    holdings = holdings_from_fills([*load_ledger_fills(state, portfolio_id), *extra_fills],
                                   positions)  # fmt: skip
    protected = [t for t, h in holdings.items() if settings_for(h.strategy_id).enabled]
    if not protected and not working:
        return StopPlan()
    windows = {settings_for(holdings[t].strategy_id).atr_window for t in protected}
    atr: dict[str, float] = {}
    closes: dict[str, float] = {}
    for window in sorted(windows):
        names = [t for t in protected if settings_for(holdings[t].strategy_id).atr_window == window]
        got_atr, got_close = load_atr(lake, names, as_of, window)
        atr.update(got_atr)
        closes.update(got_close)
    return plan_stops(
        holdings,
        working,
        settings_for=settings_for,
        atr=atr,
        closes=closes,
        portfolio_id=portfolio_id,
        as_of=as_of,
        tick_id=tick_id,
        taken=stop_ids(state, portfolio_id),
    )


# ---- at a real broker ----------------------------------------------------------------------


def send_stop_plan(
    state: SqliteState,
    broker: object,
    plan: StopPlan,
    *,
    portfolio_id: str,
    clock: Clock = SYSTEM_CLOCK,
) -> StopSync:
    """Send ``plan`` through ``broker``: cancels first, then new stops.
    Every order goes through the state machine and is synced by client id
    right after. A failure leaves the row for reconciliation and never
    raises."""
    from stonks.production.tick import _record_order

    cancelled: list[str] = []
    placed: list[str] = []
    failed: list[str] = []
    for c in plan.cancel:
        if _cancel_at_broker(state, broker, c, clock):
            cancelled.append(c.stop.client_id)
        else:
            failed.append(c.stop.client_id)
    for order in plan.place:
        cid = order.client_id
        existing = current_state(state, cid)
        if existing is not None and existing not in ("rejected", "cancelled"):
            continue  # sent before (a crash after the send)
        with state.transaction():
            _record_order(state, order, status="pending", portfolio_id=portfolio_id)
        try:
            broker.place_order(order)  # type: ignore[attr-defined]
        except OrderRejectedError as exc:
            _log.warning("stops.rejected", client_id=cid, error=str(exc))
            write_state(state, cid, "rejected", reason=str(exc)[:500], clock=clock)
            failed.append(cid)
            continue
        except Exception as exc:
            _log.warning("stops.no_answer", client_id=cid, error=str(exc))
            mark_unknown(state, cid, f"submit outcome unknown: {exc}"[:500], clock=clock)
            failed.append(cid)
            continue
        write_state(state, cid, "submitted", clock=clock)
        placed.append(cid)
        if isinstance(broker, OrderStateSource):
            try:
                reconcile_order(broker, state, cid, reject_unknown=False)
            except Exception as exc:
                _log.warning("stops.sync_failed", client_id=cid, error=str(exc))
    sync = StopSync(
        placed=tuple(placed),
        cancelled=tuple(cancelled),
        kept=len(plan.keep),
        failed=tuple(failed),
        unprotected=plan.unprotected,
    )
    _log.info("stops.synced", portfolio_id=portfolio_id, **sync.as_dict())
    return sync


def _cancel_at_broker(state: SqliteState, broker: object, c: StopCancel, clock: Clock) -> bool:
    cid = c.stop.client_id
    words = CANCEL_WORDS.get(c.reason, c.reason)
    current = current_state(state, cid)
    if current is None or current in TERMINAL:
        return True
    if current == "unknown":
        return False  # reconciliation settles it first
    if not isinstance(broker, OrderCanceller):
        _log.warning("stops.cancel_unsupported", client_id=cid)
        return False
    try:
        requested = broker.cancel_order(cid)
    except Exception as exc:
        _log.warning("stops.cancel_failed", client_id=cid, error=str(exc))
        mark_unknown(state, cid, f"cancel outcome unknown: {exc}"[:500], clock=clock)
        return False
    if requested and can_transition(current, "pending_cancel"):  # type: ignore[arg-type]
        write_state(state, cid, "pending_cancel", reason=words, clock=clock)
    if isinstance(broker, OrderStateSource):
        try:
            reconcile_order(broker, state, cid, reject_unknown=False)
        except Exception as exc:
            _log.warning("stops.sync_failed", client_id=cid, error=str(exc))
    after = current_state(state, cid)
    if after is not None and after not in TERMINAL and not requested:
        # the broker has nothing working under this id
        if can_transition(after, "cancelled"):
            write_state(state, cid, "cancelled", reason=words, clock=clock)
    elif after == "cancelled":
        state.execute("UPDATE orders SET status_reason = ? WHERE client_id = ?", [words, cid])
    return True


# ---- a simulated book --------------------------------------------------------------------


def record_paper_plan(state: SqliteState, plan: StopPlan, *, portfolio_id: str) -> StopSync:
    """Write ``plan`` for a simulated book, whose stops live in the ledger:
    new stops are working (``accepted``) rows, cancelled ones end
    ``cancelled``. Call inside the transaction that writes the run's
    snapshot."""
    from stonks.production.tick import _record_order

    cancelled: list[str] = []
    for c in plan.cancel:
        current = current_state(state, c.stop.client_id)
        if current is None or not can_transition(current, "cancelled"):
            continue
        write_state(
            state, c.stop.client_id, "cancelled", reason=CANCEL_WORDS.get(c.reason, c.reason)
        )
        cancelled.append(c.stop.client_id)
    placed: list[str] = []
    for order in plan.place:
        if current_state(state, order.client_id) is not None:
            continue
        _record_order(state, order, status="pending", portfolio_id=portfolio_id)
        write_state(state, order.client_id, "accepted")
        placed.append(order.client_id)
    return StopSync(
        placed=tuple(placed),
        cancelled=tuple(cancelled),
        kept=len(plan.keep),
        unprotected=plan.unprotected,
    )


def _stop_order(row: Any, portfolio_id: str) -> Order:
    decided = row["decided_at"]
    return Order(
        client_id=row["client_id"],
        ticker=row["ticker"],
        side=row["side"],
        quantity=float(row["quantity"]),
        order_type=row["order_type"],
        stop_price=float(row["stop_price"]),
        limit_price=row["limit_price"],
        time_in_force="gtc",
        position_effect="close",
        strategy_id=row["strategy_id"],
        tick_id=row["tick_id"],
        portfolio_id=portfolio_id,
        oca_group=row["oca_group"],
        decided_at=datetime.fromisoformat(decided) if decided else None,
        decision_context=_context(row["decision_context_json"]),
    )


def sweep_paper_stops(
    state: SqliteState,
    lake: DuckDBLake | None,
    portfolio: Portfolio,
    *,
    portfolio_id: str,
    as_of: date,
    make_broker: Callable[[Portfolio], SimulatedBroker] | None = None,
) -> list[tuple[Order, Fill]]:
    """Fill a simulated book's working stops from the daily bars after the
    day each was placed, up to ``as_of``, through the simulated broker's
    resting stops. ``portfolio`` changes in place. Returns each stop that
    filled (as filled) with its fill, for the caller to record with the
    run's snapshot."""
    from stonks.backtest.simulated_broker import SimulatedBroker

    if lake is None or not stops_recorded(state):
        return []
    marks = ",".join("?" for _ in NON_TERMINAL_STATUSES)
    rows = state.sql(
        "SELECT client_id, ticker, side, quantity, order_type, stop_price, limit_price,"
        " strategy_id, tick_id, oca_group, decided_at, decision_context_json, created_at"
        f" FROM orders WHERE portfolio_id = ? AND protective = 1 AND status IN ({marks})"
        " AND stop_price IS NOT NULL ORDER BY created_at, rowid",
        [portfolio_id, *NON_TERMINAL_STATUSES],
    )
    if not rows:
        return []
    stops = [(_stop_order(r, portfolio_id), _placed_day(r)) for r in rows]
    first = min(day for _, day in stops) + timedelta(days=1)
    if first > as_of:
        return []
    bars = lake.sql(
        "SELECT ticker, CAST(timestamp AS DATE) AS date, open, high, low FROM bars"
        " WHERE interval = '1d' AND ticker = ANY(?) AND timestamp >= ? AND timestamp < ?"
        " ORDER BY timestamp, ticker",
        [
            sorted({o.ticker for o, _ in stops}),
            day_start(first),
            day_start(as_of + timedelta(days=1)),
        ],
    )
    broker = make_broker(portfolio) if make_broker is not None else SimulatedBroker(portfolio)
    by_id = {o.client_id: o for o, _ in stops}
    fills: list[Fill] = []
    for day, group in bars.groupby("date", sort=True):
        bar_day = day.date() if hasattr(day, "date") else day
        for order, placed in stops:
            if placed < bar_day:
                broker.place_order(order)  # rests; a no-op once resting or filled
        opens = {str(r.ticker): float(r.open) for r in group.itertuples() if r.open == r.open}
        highs = {str(r.ticker): float(r.high) for r in group.itertuples() if r.high == r.high}
        lows = {str(r.ticker): float(r.low) for r in group.itertuples() if r.low == r.low}
        broker.set_prices(opens, bar_day, highs=highs, lows=lows)
        fills.extend(broker.trigger_resting())
    out: list[tuple[Order, Fill]] = []
    for f in fills:
        order = by_id[f.order_client_id]
        out.append((replace(order, quantity=f.quantity), f))
        _log.info(
            "stops.paper_filled",
            portfolio_id=portfolio_id,
            client_id=f.order_client_id,
            price=f.price,
            quantity=f.quantity,
        )
    # a stop the bars could not fill but whose position is gone is cancelled
    # by the run's plan, not here
    return out


def _placed_day(row: Any) -> date:
    raw = row["decided_at"] or row["created_at"]
    return datetime.fromisoformat(str(raw)).date()
