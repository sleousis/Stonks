"""Earnings that land before the next market open (roadmap 20.7).

A position held through an earnings report can gap at the next open. The
order ticket warns when a ticker reports between now and that open.

A report's moment comes from its session and timing: ``before`` just
before the open, ``after`` just after the close, ``during`` mid-session.
With no timing the whole report day counts (the safe side). A report on a
day with no session (a weekend) counts for that whole day too. It falls
before the next open when that moment lies between ``now`` and the next
session open of the ticker's market.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, time, timedelta

from pydantic import BaseModel

from stonks.calendars.models import EarningsEvent
from stonks.ingest.calendar_schemas import ReportTiming
from stonks.scheduling.calendar import (
    MarketCalendar,
    UnknownCalendarError,
    ensure_utc,
    get_calendar,
)

#: The market used when a ticker's exchange has no calendar mapped.
FALLBACK_CALENDAR = "XNYS"
_EDGE = timedelta(minutes=1)


class EarningsWarning(BaseModel):
    ticker: str
    name: str | None = None
    report_date: date
    before_after_market: ReportTiming | None = None
    #: The next session open of the ticker's market (UTC).
    next_open: datetime


def _window(cal: MarketCalendar, day: date, timing: str | None) -> tuple[datetime, datetime]:
    """When the report may land, as a UTC interval."""
    session = cal.session(day)
    if session is None:
        start = datetime.combine(day, time.min, tzinfo=UTC)
        return start, start + timedelta(days=1) - _EDGE
    if timing == "before":
        moment = session.open - _EDGE
    elif timing == "after":
        moment = session.close + _EDGE
    elif timing == "during":
        moment = session.open + (session.close - session.open) / 2
    else:
        return session.open - _EDGE, session.close + _EDGE
    return moment, moment


def _calendar(ticker: str, calendar_for: Callable[[str], MarketCalendar]) -> MarketCalendar:
    try:
        return calendar_for(ticker)
    except UnknownCalendarError:
        return get_calendar(FALLBACK_CALENDAR)


def earnings_before_next_open(
    events: Iterable[EarningsEvent],
    *,
    now: datetime,
    calendar_for: Callable[[str], MarketCalendar],
) -> list[EarningsWarning]:
    """The events among ``events`` whose report falls between ``now``
    (timezone aware) and the next open of their market, in the given order."""
    now = ensure_utc(now)
    out: list[EarningsWarning] = []
    for event in events:
        cal = _calendar(event.ticker, calendar_for)
        next_open = cal.next_open(now)
        lo, hi = _window(cal, event.report_date, event.before_after_market)
        if hi >= now and lo <= next_open:
            out.append(
                EarningsWarning(
                    ticker=event.ticker,
                    name=event.name,
                    report_date=event.report_date,
                    before_after_market=event.before_after_market,
                    next_open=next_open,
                )
            )
    return out
