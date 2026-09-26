"""Daily P&L from portfolio snapshots (roadmap 2.5c), behind ``stonks pnl``.

The real portfolio has one row per UTC calendar day: the last
``portfolio_snapshots`` row taken that day (a same-day rerun supersedes the
earlier run). A shadow strategy's virtual portfolio already has one row per
``as_of`` in ``shadow_portfolio_snapshots``.

Returns and drawdown are measured from inception (the first snapshot) even
when ``since`` trims the displayed window, so a row's numbers don't change
with the window you ask for.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

from stonks.store.state import SqliteState


@dataclass(frozen=True)
class PnlRow:
    day: date
    total_value: float
    daily_change: float | None
    daily_return: float | None
    cumulative_return: float | None
    drawdown: float  # <= 0, relative to the running peak


def daily_pnl(points: Sequence[tuple[date, float]], since: date | None = None) -> list[PnlRow]:
    rows: list[PnlRow] = []
    if not points:
        return rows
    base = points[0][1]
    peak = float("-inf")
    prev: float | None = None
    for day, value in points:
        peak = max(peak, value)
        change = None if prev is None else value - prev
        daily_return = None if prev is None or prev == 0 else value / prev - 1
        cumulative = None if base == 0 else value / base - 1
        drawdown = 0.0 if peak <= 0 else min(value / peak - 1, 0.0)
        rows.append(
            PnlRow(
                day=day,
                total_value=value,
                daily_change=change,
                daily_return=daily_return,
                cumulative_return=cumulative,
                drawdown=drawdown,
            )
        )
        prev = value
    if since is not None:
        rows = [r for r in rows if r.day >= since]
    return rows


def load_pnl(
    state: SqliteState, since: date | None = None, strategy_id: str | None = None
) -> list[PnlRow]:
    """Real portfolio P&L, or a shadow strategy's virtual P&L when
    ``strategy_id`` is given."""
    if strategy_id is not None:
        rows = state.sql(
            "SELECT as_of, total_value FROM shadow_portfolio_snapshots "
            "WHERE strategy_id = ? ORDER BY as_of",
            [strategy_id],
        )
        points = [(date.fromisoformat(r["as_of"]), float(r["total_value"])) for r in rows]
        return daily_pnl(points, since=since)

    rows = state.sql("SELECT taken_at, total_value FROM portfolio_snapshots ORDER BY id")
    by_day: dict[date, tuple[datetime, float]] = {}
    for r in rows:
        taken = _utc(r["taken_at"])
        day = taken.date()
        if day not in by_day or taken >= by_day[day][0]:
            by_day[day] = (taken, float(r["total_value"]))
    points = [(day, value) for day, (_, value) in sorted(by_day.items())]
    return daily_pnl(points, since=since)


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
