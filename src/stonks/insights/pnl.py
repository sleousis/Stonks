"""P&L over standard periods from a book's daily values."""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from datetime import date, timedelta

from stonks.insights.models import Period, PeriodPnl

#: Calendar days back from the last value for each fixed period.
_DAYS: dict[Period, int] = {"1w": 7, "1m": 30, "3m": 91, "1y": 365}


def _row(period: Period, start: tuple[date, float] | None, end: tuple[date, float]) -> PeriodPnl:
    if start is None:
        return PeriodPnl(
            period=period,
            start_day=None,
            end_day=end[0],
            start_value=None,
            end_value=end[1],
            change=None,
            change_pct=None,
        )
    change = end[1] - start[1]
    return PeriodPnl(
        period=period,
        start_day=start[0],
        end_day=end[0],
        start_value=start[1],
        end_value=end[1],
        change=change,
        change_pct=change / start[1] if start[1] else None,
    )


def period_pnl(points: Sequence[tuple[date, float]]) -> list[PeriodPnl]:
    """For each period, the change from the last value on or before the
    period's start to the latest value. ``points`` are ``(day, value)``,
    oldest first. A period longer than the history has no start."""
    if not points:
        return []
    days = [d for d, _ in points]
    end = points[-1]

    def on_or_before(day: date) -> tuple[date, float] | None:
        i = bisect_right(days, day)
        return points[i - 1] if i else None

    rows = [_row("1d", points[-2] if len(points) > 1 else None, end)]
    for period in ("1w", "1m", "3m"):
        rows.append(_row(period, on_or_before(end[0] - timedelta(days=_DAYS[period])), end))
    rows.append(_row("ytd", on_or_before(date(end[0].year - 1, 12, 31)), end))
    rows.append(_row("1y", on_or_before(end[0] - timedelta(days=_DAYS["1y"])), end))
    rows.append(_row("inception", points[0], end))
    return rows
