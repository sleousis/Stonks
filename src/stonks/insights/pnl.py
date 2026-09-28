"""P&L over standard periods from a book's daily values."""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from datetime import date, timedelta

from stonks.insights.models import Period, PeriodPnl
from stonks.insights.returns import Flow, twr

#: Calendar days back from the last value for each fixed period.
_DAYS: dict[Period, int] = {"1w": 7, "1m": 30, "3m": 91, "1y": 365}


def _row(
    period: Period,
    start: tuple[date, float] | None,
    end: tuple[date, float],
    points: Sequence[tuple[date, float]] = (),
    flows: Sequence[Flow] = (),
) -> PeriodPnl:
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
    window = [p for p in points if start[0] <= p[0] <= end[0]] or [start, end]
    inside = sum(f.amount for f in flows if start[0] < f.day <= end[0])
    return PeriodPnl(
        period=period,
        start_day=start[0],
        end_day=end[0],
        start_value=start[1],
        end_value=end[1],
        change=change,
        change_pct=change / start[1] if start[1] else None,
        net_flows=inside,
        twr=twr(window, flows),
    )


#: Longest gap in calendar days the ``1d`` row spans (a long weekend). The
#: app passes ``production.pnl.DEFAULT_MAX_GAP_DAYS`` so the ``1d`` row and
#: the headline day change follow one rule.
MAX_1D_GAP_DAYS = 4


def period_pnl(
    points: Sequence[tuple[date, float]],
    flows: Sequence[Flow] = (),
    *,
    max_gap_days: int = MAX_1D_GAP_DAYS,
) -> list[PeriodPnl]:
    """For each period, the change from the last value on or before the
    period's start to the latest value, and the time-weighted return with
    ``flows`` (deposits and withdrawals) taken out. ``points`` are
    ``(day, value)``, oldest first. A period longer than the history has no
    start, and ``1d`` has none across a gap longer than ``max_gap_days``."""
    if not points:
        return []
    days = [d for d, _ in points]
    end = points[-1]

    def on_or_before(day: date) -> tuple[date, float] | None:
        i = bisect_right(days, day)
        return points[i - 1] if i else None

    def row(period: Period, start: tuple[date, float] | None) -> PeriodPnl:
        return _row(period, start, end, points, flows)

    before = points[-2] if len(points) > 1 else None
    if before is not None and (end[0] - before[0]).days > max_gap_days:
        before = None
    rows = [row("1d", before)]
    for period in ("1w", "1m", "3m"):
        rows.append(row(period, on_or_before(end[0] - timedelta(days=_DAYS[period]))))
    rows.append(row("ytd", on_or_before(date(end[0].year - 1, 12, 31))))
    rows.append(row("1y", on_or_before(end[0] - timedelta(days=_DAYS["1y"]))))
    rows.append(row("inception", points[0]))
    return rows
