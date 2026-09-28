"""Round trips of a paper or live book, built from its fills (roadmap 23.3).

The pairing is the backtest's (:func:`stonks.backtest.trades.build_round_trips`):
FIFO per ticker, long and short lots, splits and dividends applied, one
round trip per (lot, closing fill). The journal adds what a person reviews:

- **Trade and leg.** A trade is one opening fill (a lot) and its id is that
  fill's id. Each closing fill of the lot is a leg (``"<trade>.<n>"``), and
  what is still held is the last, open, leg.
- **Sleeve.** A lot belongs to the strategy whose order opened it, or to
  ``manual`` for a person's own orders, so manual trades stay apart. A fill
  closes its own sleeve's lots first, then the oldest of any other.
- **Holding time** in days, from the entry fill to the exit fill (or now).
- **Excursions.** The worst (MAE) and best (MFE) move from the entry over
  the daily bars from the entry day to the exit day, bounded by zero. With
  daily bars the entry day's whole range counts.
- **R multiple.** P&L over the money at risk at the initial stop, when a
  :mod:`stop source <stonks.journal.stops>` finds one on the right side of
  the entry. ``mae_r`` is the worst excursion in the same unit.
- **Exit efficiency.** Where the exit sits in the range the trade saw, from
  0 (the worst price) to 1 (the best): ``(move - MAE) / (MFE - MAE)``.
- **Exit trigger.** ``stop`` for a stop order, else the exit order's
  recorded trigger (``signal``, ``exit_no_pick``, ``risk_rule``,
  ``manual``), else ``manual`` for a person's order.

Option contracts are left out: their P&L needs the contract multiplier.
Money is in the instrument's own currency; the service converts it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from functools import cached_property
from typing import TYPE_CHECKING, Any, Literal

import pandas as pd

from stonks.backtest.corporate_actions import CorporateActionRecord
from stonks.backtest.trades import Excursions, RoundTrip, TradeSide, build_round_trips
from stonks.core.options import is_option_id
from stonks.core.types import Fill, OrderSide
from stonks.journal.stops import FoundStop, StopSource, find_stop

if TYPE_CHECKING:
    from stonks.store.state import SqliteState

Origin = Literal["strategy", "manual"]
#: The sleeve of a person's own orders.
MANUAL = "manual"
#: The sleeve of a strategy order that names no strategy.
UNATTRIBUTED = "unattributed"
STOP_TYPES = frozenset({"stop", "stop_limit"})


@dataclass(frozen=True)
class LedgerFill:
    fill_id: int
    client_id: str
    ticker: str
    side: OrderSide
    quantity: float
    price: float
    fee: float
    filled_at: datetime
    strategy_id: str | None
    origin: Origin = "strategy"


@dataclass(frozen=True)
class LedgerOrder:
    client_id: str
    ticker: str
    side: OrderSide
    order_type: str
    stop_price: float | None
    protective: bool
    context: Mapping[str, Any]
    created_at: datetime
    origin: Origin = "strategy"


@dataclass(frozen=True)
class Ledger:
    """One book's fills, oldest first, its orders by client id, and the
    corporate actions applied to it."""

    fills: tuple[LedgerFill, ...]
    orders: Mapping[str, LedgerOrder] = field(default_factory=dict[str, LedgerOrder])
    corporate_actions: tuple[CorporateActionRecord, ...] = ()

    @cached_property
    def stop_orders(self) -> tuple[LedgerOrder, ...]:
        return tuple(o for o in self.orders.values() if o.order_type in STOP_TYPES)


@dataclass(frozen=True)
class JournalTrade:
    """One leg of a trade: a lot from its entry to one exit, or still open."""

    leg_id: str
    trade_id: int
    ticker: str
    side: TradeSide
    sleeve: str
    origin: Origin
    entry_client_id: str
    exit_client_id: str | None
    entry_at: datetime
    exit_at: datetime | None
    quantity: float
    entry_price: float
    #: The exit fill's price, or the latest close for an open leg.
    exit_price: float
    is_open: bool
    holding_days: float
    pnl: float
    return_pct: float
    fees: float
    dividends: float
    mae_pct: float | None
    mfe_pct: float | None
    exit_efficiency: float | None
    exit_trigger: str | None
    stop_price: float | None
    stop_source: str | None
    target_price: float | None
    risk_amount: float | None
    r_multiple: float | None
    mae_r: float | None


def build_trades(
    ledger: Ledger,
    *,
    bars: pd.DataFrame | None,
    now: datetime,
    marks: Mapping[str, float] | None = None,
    stop_sources: list[StopSource] | None = None,
) -> list[JournalTrade]:
    """Every leg of ``ledger``: closed legs by exit, then open legs by entry.

    ``bars`` has columns ``ticker, timestamp, high, low, close`` (daily) and
    feeds the excursions and the open-leg mark (``marks`` wins)."""
    fills = [f for f in ledger.fills if f.quantity > 0 and not is_option_id(f.ticker)]
    if not fills:
        return []
    rows: dict[int, LedgerFill] = {}
    core: list[Fill] = []
    for row in fills:
        fill = Fill(
            order_client_id=row.client_id,
            ticker=row.ticker,
            quantity=row.quantity,
            price=row.price,
            fee=row.fee,
            filled_at=row.filled_at,
            side=row.side,
        )
        rows[id(fill)] = row
        core.append(fill)
    excursions = Excursions(bars)
    last_marks = dict(marks or {})
    for ticker in {f.ticker for f in fills} - set(last_marks):
        close = excursions.last_close(ticker)
        if close is not None:
            last_marks[ticker] = close
    trips = build_round_trips(
        core,
        timeline=[now],
        corporate_actions=ledger.corporate_actions,
        marks=last_marks,
        key_of=lambda f: sleeve_of(rows[id(f)]),
        ref_of=lambda f: str(rows[id(f)].fill_id),
    )
    by_id = {r.fill_id: r for r in fills}
    splits: dict[str, list[tuple[datetime, float]]] = {}
    for action in ledger.corporate_actions:
        if action.kind == "split":
            splits.setdefault(action.ticker, []).append((_utc(action.timestamp), action.value))
    sources = stop_sources
    legs: dict[int, int] = {}
    out: list[JournalTrade] = []
    for trip in trips:
        entry = by_id[int(trip.entry_ref)]
        exit_row = by_id[int(trip.exit_ref)] if trip.exit_ref else None
        legs[entry.fill_id] = legs.get(entry.fill_id, 0) + 1
        out.append(
            _leg(
                trip,
                entry,
                exit_row,
                leg=legs[entry.fill_id],
                ledger=ledger,
                excursions=excursions,
                splits=splits.get(trip.ticker, []),
                now=now,
                sources=sources,
            )
        )
    return out


def sleeve_of(row: LedgerFill) -> str:
    if row.origin == "manual":
        return MANUAL
    return row.strategy_id or UNATTRIBUTED


def _leg(
    trip: RoundTrip,
    entry: LedgerFill,
    exit_row: LedgerFill | None,
    *,
    leg: int,
    ledger: Ledger,
    excursions: Excursions,
    splits: Sequence[tuple[datetime, float]],
    now: datetime,
    sources: list[StopSource] | None,
) -> JournalTrade:
    direction = 1 if trip.side == "long" else -1
    entry_at = _utc(trip.entry_ts)
    exit_at = None if trip.is_open else _utc(trip.exit_ts)
    until = exit_at or now
    mae, mfe = excursions.mae_mfe(
        trip.ticker,
        datetime.combine(entry_at.date(), time.min, UTC),
        datetime.combine(until.date(), time.max, UTC),
        trip.entry_px,
        splits,
        direction=direction,
    )
    efficiency = None
    if not trip.is_open and mae is not None and mfe is not None and mfe > mae:
        move = direction * (trip.exit_px / trip.entry_px - 1.0)
        efficiency = min(1.0, max(0.0, (move - mae) / (mfe - mae)))
    entry_order = ledger.orders.get(entry.client_id)
    found = find_stop(entry_order, ledger, sources) if entry_order is not None else None
    stop, target, source = _adjusted(found, splits, entry_at, until)
    risk_per_share = direction * (trip.entry_px - stop) if stop is not None else None
    if risk_per_share is not None and risk_per_share <= 0:
        stop, target, source, risk_per_share = None, None, None, None
    risk = risk_per_share * trip.qty if risk_per_share is not None else None
    return JournalTrade(
        leg_id=f"{entry.fill_id}.{leg}",
        trade_id=entry.fill_id,
        ticker=trip.ticker,
        side=trip.side,
        sleeve=trip.strategy_key,
        origin=entry.origin,
        entry_client_id=entry.client_id,
        exit_client_id=exit_row.client_id if exit_row is not None else None,
        entry_at=entry_at,
        exit_at=exit_at,
        quantity=trip.qty,
        entry_price=trip.entry_px,
        exit_price=trip.exit_px,
        is_open=trip.is_open,
        holding_days=max((until - entry_at).total_seconds(), 0.0) / 86400.0,
        pnl=trip.pnl,
        return_pct=trip.return_pct,
        fees=trip.fees,
        dividends=trip.dividends,
        mae_pct=mae,
        mfe_pct=mfe,
        exit_efficiency=efficiency,
        exit_trigger=_exit_trigger(ledger, exit_row),
        stop_price=stop,
        stop_source=source,
        target_price=target,
        risk_amount=risk,
        r_multiple=trip.pnl / risk if risk else None,
        mae_r=(mae * trip.entry_px / risk_per_share)
        if mae is not None and risk_per_share
        else None,
    )


def _adjusted(
    found: FoundStop | None,
    splits: Sequence[tuple[datetime, float]],
    entry_at: datetime,
    until: datetime,
) -> tuple[float | None, float | None, str | None]:
    """The stop and target in post-split prices, like the lot."""
    if found is None:
        return None, None, None
    factor = 1.0
    for ts, ratio in splits:
        if entry_at < ts <= until and ratio > 0:
            factor *= ratio
    target = found.target / factor if found.target is not None else None
    return found.price / factor, target, found.source


def _exit_trigger(ledger: Ledger, exit_row: LedgerFill | None) -> str | None:
    if exit_row is None:
        return None
    order = ledger.orders.get(exit_row.client_id)
    if order is not None:
        if order.order_type in STOP_TYPES:
            return "stop"
        trigger = order.context.get("trigger")
        if isinstance(trigger, str) and trigger:
            return trigger
    return MANUAL if exit_row.origin == "manual" else None


def _utc(value: datetime) -> datetime:
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()  # type: ignore[union-attr]
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def parse_ts(value: str) -> datetime:
    """An ISO timestamp from the ledger (naive means UTC)."""
    ts = datetime.fromisoformat(value)
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def _context(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def load_ledger(state: SqliteState, portfolio_id: str) -> Ledger:
    """One portfolio's fills, orders and applied corporate actions."""
    fill_rows = state.sql(
        "SELECT f.id, f.order_client_id, f.ticker, f.quantity, f.price, f.fee, f.filled_at,"
        " o.side, o.strategy_id, o.origin FROM fills f"
        " JOIN orders o ON o.client_id = f.order_client_id"
        " WHERE COALESCE(f.portfolio_id, o.portfolio_id) = ?"
        " ORDER BY f.filled_at, f.id",
        [portfolio_id],
    )
    fills = tuple(
        LedgerFill(
            fill_id=int(r["id"]),
            client_id=str(r["order_client_id"]),
            ticker=str(r["ticker"]),
            side=r["side"],
            quantity=float(r["quantity"]),
            price=float(r["price"]),
            fee=float(r["fee"] or 0.0),
            filled_at=parse_ts(r["filled_at"]),
            strategy_id=r["strategy_id"],
            origin="manual" if r["origin"] == "manual" else "strategy",
        )
        for r in fill_rows
    )
    order_rows = state.sql(
        "SELECT client_id, ticker, side, order_type, stop_price, protective, origin,"
        " decision_context_json, created_at FROM orders WHERE portfolio_id = ?",
        [portfolio_id],
    )
    orders = {
        str(r["client_id"]): LedgerOrder(
            client_id=str(r["client_id"]),
            ticker=str(r["ticker"]),
            side=r["side"],
            order_type=str(r["order_type"]),
            stop_price=float(r["stop_price"]) if r["stop_price"] is not None else None,
            protective=bool(r["protective"]),
            context=_context(r["decision_context_json"]),
            created_at=parse_ts(r["created_at"]),
            origin="manual" if r["origin"] == "manual" else "strategy",
        )
        for r in order_rows
    }
    action_rows = state.sql(
        "SELECT ticker, ex_date, kind, value, quantity_before, quantity_after, cash_delta"
        " FROM corporate_action_ledger WHERE portfolio_id = ? AND quantity_before != 0"
        " ORDER BY ex_date",
        [portfolio_id],
    )
    actions = tuple(
        CorporateActionRecord(
            timestamp=datetime.combine(datetime.fromisoformat(r["ex_date"]).date(), time.min, UTC),
            ticker=str(r["ticker"]),
            kind=r["kind"],
            value=float(r["value"]),
            quantity_before=float(r["quantity_before"]),
            quantity_after=float(r["quantity_after"]),
            cash_delta=float(r["cash_delta"]),
        )
        for r in action_rows
    )
    return Ledger(fills=fills, orders=orders, corporate_actions=actions)
