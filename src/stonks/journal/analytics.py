"""Results of journal trades by group, and the P&L calendar (roadmap 23.3).

Win and loss figures count closed legs only: an open leg's P&L is not
realised yet (``open`` counts them). A trade in several groups (two tags)
counts once in each. The calendar books each closed leg's realised P&L on
its exit day (UTC), then sums ISO weeks (Monday to Sunday) and months.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from stonks.backtest import metrics
from stonks.journal.trips import JournalTrade

KeysOf = Callable[[JournalTrade], Iterable[str]]
AmountOf = Callable[[JournalTrade], float | None]


@dataclass(frozen=True)
class GroupStats:
    key: str
    #: Closed legs.
    trades: int
    wins: int
    losses: int
    open: int
    win_rate: float | None
    pnl: float
    avg_pnl: float | None
    avg_win: float | None
    #: Mean P&L of the losing legs (<= 0).
    avg_loss: float | None
    profit_factor: float | None
    #: Mean R multiple over the closed legs that have one.
    avg_r: float | None
    r_trades: int
    avg_holding_days: float | None
    avg_exit_efficiency: float | None


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _stats(key: str, legs: Sequence[JournalTrade]) -> GroupStats:
    closed = [t for t in legs if not t.is_open]
    pnls = [t.pnl for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    rs = [t.r_multiple for t in closed if t.r_multiple is not None]
    eff = [t.exit_efficiency for t in closed if t.exit_efficiency is not None]
    pf = metrics.profit_factor(pnls) if closed else None
    return GroupStats(
        key=key,
        trades=len(closed),
        wins=len(wins),
        losses=len(losses),
        open=len(legs) - len(closed),
        win_rate=len(wins) / len(closed) if closed else None,
        pnl=sum(pnls),
        avg_pnl=_mean(pnls),
        avg_win=_mean(wins),
        avg_loss=_mean(losses),
        profit_factor=pf if pf is None or pf != float("inf") else None,
        avg_r=_mean(rs),
        r_trades=len(rs),
        avg_holding_days=_mean([t.holding_days for t in closed]),
        avg_exit_efficiency=_mean(eff),
    )


def group_stats(trades: Iterable[JournalTrade], keys_of: KeysOf) -> list[GroupStats]:
    """Stats per key, most closed legs first. ``keys_of`` names each leg's
    groups (one or several)."""
    groups: dict[str, list[JournalTrade]] = {}
    for trade in trades:
        for key in dict.fromkeys(keys_of(trade)):
            groups.setdefault(key, []).append(trade)
    stats = [_stats(k, v) for k, v in groups.items()]
    return sorted(stats, key=lambda g: (-g.trades, -g.open, g.key))


# ---- the P&L calendar ------------------------------------------------------------


@dataclass(frozen=True)
class CalendarBucket:
    #: ``YYYY-MM-DD``, ``YYYY-Www`` (ISO week) or ``YYYY-MM``.
    key: str
    start: date
    end: date
    pnl: float
    trades: int
    wins: int


@dataclass(frozen=True)
class PnlCalendar:
    days: list[CalendarBucket]
    weeks: list[CalendarBucket]
    months: list[CalendarBucket]
    total: float
    trades: int
    best_day: str | None
    worst_day: str | None
    #: Closed legs left out because their amount is unknown (no FX rate).
    unconverted: int


def _week(day: date) -> tuple[str, date, date]:
    year, week, _ = day.isocalendar()
    start = day - timedelta(days=day.weekday())
    return f"{year}-W{week:02d}", start, start + timedelta(days=6)


def _month(day: date) -> tuple[str, date, date]:
    start = day.replace(day=1)
    following = (start + timedelta(days=32)).replace(day=1)
    return f"{day.year}-{day.month:02d}", start, following - timedelta(days=1)


def _bucket(
    rows: Sequence[tuple[date, float]], period: Callable[[date], tuple[str, date, date]]
) -> list[CalendarBucket]:
    sums: dict[str, list[float]] = {}
    spans: dict[str, tuple[date, date]] = {}
    for day, amount in rows:
        key, start, end = period(day)
        slot = sums.setdefault(key, [0.0, 0, 0])
        slot[0] += amount
        slot[1] += 1
        slot[2] += 1 if amount > 0 else 0
        spans[key] = (start, end)
    return [
        CalendarBucket(k, spans[k][0], spans[k][1], v[0], int(v[1]), int(v[2]))
        for k, v in sorted(sums.items(), key=lambda kv: spans[kv[0]][0])
    ]


def pnl_calendar(trades: Iterable[JournalTrade], amount_of: AmountOf) -> PnlCalendar:
    """Realised P&L of closed legs by exit day, ISO week and month.
    ``amount_of`` gives a leg's P&L in the reporting currency, or ``None``
    when it cannot be converted (the leg is then left out and counted)."""
    rows: list[tuple[date, float]] = []
    unconverted = 0
    for trade in trades:
        if trade.is_open or trade.exit_at is None:
            continue
        amount = amount_of(trade)
        if amount is None:
            unconverted += 1
            continue
        rows.append((trade.exit_at.date(), amount))
    days = _bucket(rows, lambda d: (d.isoformat(), d, d))
    best = max(days, key=lambda b: b.pnl).key if days else None
    worst = min(days, key=lambda b: b.pnl).key if days else None
    return PnlCalendar(
        days=days,
        weeks=_bucket(rows, _week),
        months=_bucket(rows, _month),
        total=sum(a for _, a in rows),
        trades=len(rows),
        best_day=best,
        worst_day=worst,
        unconverted=unconverted,
    )
