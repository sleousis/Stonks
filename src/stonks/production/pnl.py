"""Daily P&L from portfolio snapshots (roadmap 2.5c), behind ``stonks pnl``.

Both the real portfolio and a shadow strategy's virtual portfolio are keyed
by the tick's trading date (``as_of``), so their rows line up day for day:

- real: the last ``portfolio_snapshots`` row per ``as_of`` (a same-day
  rerun supersedes the earlier run). Rows written before ``as_of`` existed
  fall back to the UTC date of ``taken_at``;
- shadow: ``shadow_portfolio_snapshots`` already has one row per ``as_of``.

``daily_change`` / ``daily_return`` compare a row with the previous row and
``days_elapsed`` says how many calendar days apart they are. When more than
``max_gap_days`` (default 4, a long weekend) separate the two, the daily
fields are left ``None``: a return over missed ticks is not a daily return.

Returns and drawdown are measured from inception (the first snapshot) even
when ``since`` trims the displayed window, so a row's numbers don't change
with the window you ask for.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

from stonks.store.state import SqliteState

DEFAULT_MAX_GAP_DAYS = 4


@dataclass(frozen=True)
class PnlRow:
    day: date
    total_value: float
    daily_change: float | None
    daily_return: float | None
    cumulative_return: float | None
    drawdown: float  # <= 0, relative to the running peak
    days_elapsed: int | None = None  # calendar days since the previous row


def daily_pnl(
    points: Sequence[tuple[date, float]],
    since: date | None = None,
    *,
    max_gap_days: int = DEFAULT_MAX_GAP_DAYS,
) -> list[PnlRow]:
    rows: list[PnlRow] = []
    if not points:
        return rows
    base = points[0][1]
    peak = float("-inf")
    prev: tuple[date, float] | None = None
    for day, value in points:
        peak = max(peak, value)
        elapsed = None if prev is None else (day - prev[0]).days
        # Only compare with a previous row inside the gap limit.
        prev_value = prev[1] if prev is not None and elapsed <= max_gap_days else None
        change = None if prev_value is None else value - prev_value
        daily_return = None if not prev_value else value / prev_value - 1
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
                days_elapsed=elapsed,
            )
        )
        prev = (day, value)
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

    rows = state.sql("SELECT as_of, taken_at, total_value FROM portfolio_snapshots ORDER BY id")
    by_day: dict[date, float] = {}
    for r in rows:
        # Later ids win: a same-as_of rerun supersedes the earlier run.
        by_day[_snapshot_day(r["as_of"], r["taken_at"])] = float(r["total_value"])
    points = sorted(by_day.items())
    return daily_pnl(points, since=since)


def _snapshot_day(as_of: str | None, taken_at: str) -> date:
    if as_of:
        return date.fromisoformat(as_of)
    return _utc(taken_at).date()


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
