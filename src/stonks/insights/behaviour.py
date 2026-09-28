"""How you trade by hand (roadmap 23.5). Pure: fills in, a report out.

The fills are your own trades: manual orders in Stonks and the trades a
broker sync brought in. They pair into round trips FIFO per ticker (a buy
closes short lots first, a sell long lots). A partial exit splits a lot.
Only closed trips count, and fees are in the P&L.

The report:

- win rate and P&L, by holding time and by the weekday of the entry;
- the disposition effect: losers held longer than winners (by
  ``disposition_ratio``), the habit of cutting winners and riding losers;
- overtrading: days with ``busy_day_entries`` entries or more, and what
  those days made against the rest;
- revenge trades: entries within ``revenge_hours`` of a losing exit;
- trading against the strategies: each entry's stance from the active
  strategies' signals at the time (the caller's ``stance``), and what the
  trades against them made. A negative ``against_strategies_cost`` is what
  going against the strategies cost you.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal

__all__ = [
    "BehaviourFill",
    "BehaviourReport",
    "BehaviourSettings",
    "Bucket",
    "Disposition",
    "Overtrading",
    "RoundTrip",
    "behaviour_report",
    "round_trips",
]

FillSource = Literal["manual", "broker"]
Stance = Literal["with", "against", "no_view"]
#: ``(ticker, entry day, direction) -> stance``; direction is 1 long, -1 short.
StanceFn = Callable[[str, date, int], Stance]

_EPS = 1e-9
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
#: ``(label, upper bound in days)`` of the holding time buckets.
_HOLDING: tuple[tuple[str, float], ...] = (
    ("under a day", 1.0),
    ("1 to 5 days", 5.0),
    ("1 to 4 weeks", 28.0),
    ("1 to 3 months", 91.0),
    ("over 3 months", float("inf")),
)


@dataclass(frozen=True)
class BehaviourFill:
    id: str
    ticker: str
    side: Literal["buy", "sell"]
    quantity: float
    price: float
    fee: float
    filled_at: datetime
    source: FillSource = "manual"


@dataclass(frozen=True)
class BehaviourSettings:
    #: An entry this soon after a losing exit is a revenge trade.
    revenge_hours: float = 24.0
    #: A day with this many entries or more is a busy day.
    busy_day_entries: int = 5
    #: Losers held this many times longer than winners shows the effect.
    disposition_ratio: float = 1.5


@dataclass(frozen=True)
class RoundTrip:
    ticker: str
    #: 1 long, -1 short.
    direction: int
    quantity: float
    entry_at: datetime
    exit_at: datetime
    entry_price: float
    exit_price: float
    pnl: float

    @property
    def days_held(self) -> float:
        return (self.exit_at - self.entry_at).total_seconds() / 86_400.0


@dataclass(frozen=True)
class Bucket:
    label: str
    trades: int = 0
    pnl: float = 0.0
    win_rate: float | None = None


@dataclass(frozen=True)
class Disposition:
    avg_days_winners: float | None
    avg_days_losers: float | None
    #: Losers held this many times longer than winners.
    ratio: float | None
    present: bool


@dataclass(frozen=True)
class Overtrading:
    active_days: int
    entries_per_active_day: float | None
    max_entries_in_a_day: int
    busy_days: int
    busy_day_pnl: float
    other_day_pnl: float


@dataclass(frozen=True)
class BehaviourReport:
    trades: int
    open_positions: int
    win_rate: float | None
    total_pnl: float
    avg_win: float | None
    avg_loss: float | None
    by_holding: tuple[Bucket, ...]
    by_weekday: tuple[Bucket, ...]
    disposition: Disposition
    overtrading: Overtrading
    revenge: Bucket
    versus_strategies: tuple[Bucket, ...]
    #: P&L of the trades against the strategies (negative: what it cost).
    against_strategies_cost: float | None
    sources: dict[str, int] = field(default_factory=dict)


@dataclass
class _Lot:
    direction: int
    quantity: float
    price: float
    fee_per_share: float
    at: datetime


def round_trips(fills: Iterable[BehaviourFill]) -> tuple[list[RoundTrip], int]:
    """Closed round trips FIFO per ticker, and the count of open lots."""
    lots: dict[str, list[_Lot]] = {}
    out: list[RoundTrip] = []
    for f in sorted(fills, key=lambda x: (x.filled_at, x.id)):
        if f.quantity <= _EPS:
            continue
        direction = 1 if f.side == "buy" else -1
        fee_per_share = f.fee / f.quantity
        remaining = f.quantity
        book = lots.setdefault(f.ticker, [])
        while remaining > _EPS and book and book[0].direction == -direction:
            lot = book[0]
            qty = min(lot.quantity, remaining)
            gross = (f.price - lot.price) * qty * lot.direction
            fees = (lot.fee_per_share + fee_per_share) * qty
            out.append(
                RoundTrip(
                    ticker=f.ticker,
                    direction=lot.direction,
                    quantity=qty,
                    entry_at=lot.at,
                    exit_at=f.filled_at,
                    entry_price=lot.price,
                    exit_price=f.price,
                    pnl=gross - fees,
                )
            )
            lot.quantity -= qty
            remaining -= qty
            if lot.quantity <= _EPS:
                book.pop(0)
        if remaining > _EPS:
            book.append(_Lot(direction, remaining, f.price, fee_per_share, f.filled_at))
    return out, sum(len(v) for v in lots.values())


def _bucket(label: str, trips: Sequence[RoundTrip]) -> Bucket:
    if not trips:
        return Bucket(label=label)
    wins = sum(1 for t in trips if t.pnl > _EPS)
    return Bucket(
        label=label,
        trades=len(trips),
        pnl=sum(t.pnl for t in trips),
        win_rate=wins / len(trips),
    )


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def behaviour_report(
    fills: Sequence[BehaviourFill],
    settings: BehaviourSettings | None = None,
    *,
    stance: StanceFn | None = None,
) -> BehaviourReport:
    """The behaviour report of ``fills`` (see the module doc)."""
    cfg = settings or BehaviourSettings()
    trips, still_open = round_trips(fills)
    wins = [t for t in trips if t.pnl > _EPS]
    losses = [t for t in trips if t.pnl <= _EPS]

    by_holding: list[Bucket] = []
    low = 0.0
    for label, high in _HOLDING:
        by_holding.append(_bucket(label, [t for t in trips if low <= t.days_held < high]))
        low = high
    by_weekday = tuple(
        _bucket(name, [t for t in trips if t.entry_at.weekday() == i])
        for i, name in enumerate(_WEEKDAYS)
    )

    win_days = _mean([t.days_held for t in wins])
    loss_days = _mean([t.days_held for t in losses if t.pnl < -_EPS])
    ratio = loss_days / win_days if win_days and loss_days is not None else None
    disposition = Disposition(
        avg_days_winners=win_days,
        avg_days_losers=loss_days,
        ratio=ratio,
        present=ratio is not None and ratio >= cfg.disposition_ratio,
    )

    entries: dict[date, int] = {}
    for t in trips:
        entries[t.entry_at.date()] = entries.get(t.entry_at.date(), 0) + 1
    busy = {d for d, n in entries.items() if n >= cfg.busy_day_entries}
    overtrading = Overtrading(
        active_days=len(entries),
        entries_per_active_day=len(trips) / len(entries) if entries else None,
        max_entries_in_a_day=max(entries.values(), default=0),
        busy_days=len(busy),
        busy_day_pnl=sum(t.pnl for t in trips if t.entry_at.date() in busy),
        other_day_pnl=sum(t.pnl for t in trips if t.entry_at.date() not in busy),
    )

    window = timedelta(hours=cfg.revenge_hours)
    losing_exits = sorted(t.exit_at for t in trips if t.pnl < -_EPS)
    revenge = [
        t for t in trips if any(timedelta(0) <= t.entry_at - at <= window for at in losing_exits)
    ]

    versus: tuple[Bucket, ...] = ()
    cost: float | None = None
    if stance is not None:
        groups: dict[str, list[RoundTrip]] = {"with": [], "against": [], "no_view": []}
        for t in trips:
            groups[stance(t.ticker, t.entry_at.date(), t.direction)].append(t)
        versus = tuple(_bucket(k, v) for k, v in groups.items())
        cost = sum(t.pnl for t in groups["against"]) if groups["against"] else 0.0

    sources: dict[str, int] = {}
    for f in fills:
        sources[f.source] = sources.get(f.source, 0) + 1
    return BehaviourReport(
        trades=len(trips),
        open_positions=still_open,
        win_rate=len(wins) / len(trips) if trips else None,
        total_pnl=sum(t.pnl for t in trips),
        avg_win=_mean([t.pnl for t in wins]),
        avg_loss=_mean([t.pnl for t in losses]),
        by_holding=tuple(by_holding),
        by_weekday=by_weekday,
        disposition=disposition,
        overtrading=overtrading,
        revenge=_bucket("revenge", revenge),
        versus_strategies=versus,
        against_strategies_cost=cost,
        sources=sources,
    )
