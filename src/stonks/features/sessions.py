"""US exchange sessions: which calendar days the NYSE / Nasdaq trade.

Calendar-timed strategies (a quarterly rebalance on the last session of a
month, a weekly trade on Wednesdays) need to know a session date *before*
the bar arrives: deciding "today is the last session of May" from the data
would mean peeking at the next bar. Exchange calendars are published in
advance, so answering from them is look-ahead free.

The sessions come from the scheduler's market calendar
(:mod:`stonks.scheduling.calendar`, ``XNYS`` from ``exchange_calendars``),
the one US calendar in the system: regular holidays and the one-off
closures the exchange announced (national days of mourning, 9/11,
Hurricane Sandy). Outside that calendar's window (before 1970, after 2040)
every weekday counts as a session.

Intraday strategies (roadmap 21.3.1) need the regular session of the
ticker's own exchange: :func:`regular_session` returns its open and close
as naive UTC instants (the lake's bar stamps are naive UTC), so pre-market
and after-hours bars stay outside it, and early closes and daylight saving
move it. A ticker with no known calendar has no session.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache

__all__ = [
    "RegularSession",
    "is_session",
    "last_session_of_month",
    "previous_regular_session",
    "regular_session",
    "week_index",
    "weekly_session",
]

#: How many days back :func:`previous_regular_session` looks.
_MAX_BACK_DAYS = 15


@lru_cache(maxsize=256)
def _sessions(year: int) -> frozenset[date] | None:
    """The US session dates of ``year``; ``None`` when the calendar doesn't
    cover the whole year. The calendar seam is imported here, on first use,
    so the features block never loads the scheduler at import (BE-67)."""
    from stonks.scheduling.calendar import US_CALENDAR, get_calendar

    cal = get_calendar(US_CALENDAR)
    first, last = date(year, 1, 1), date(year, 12, 31)
    lo, hi = getattr(cal, "first_session", None), getattr(cal, "last_session", None)
    if (lo is not None and first < lo.replace(month=1, day=1)) or (hi is not None and last > hi):
        return None
    return frozenset(s.date for s in cal.sessions(first, last))


def is_session(day: date) -> bool:
    """True when the US exchanges hold a session on ``day``."""
    if day.weekday() >= 5:
        return False
    sessions = _sessions(day.year)
    return True if sessions is None else day in sessions


def last_session_of_month(year: int, month: int) -> date:
    """The last session of ``year``-``month``."""
    first_next = date(year + month // 12, month % 12 + 1, 1)
    day = first_next - timedelta(days=1)
    while not is_session(day):
        day -= timedelta(days=1)
    return day


def week_index(day: date) -> int:
    """Monday-anchored week number, continuous across years (unlike the ISO
    week, whose parity breaks after a 53-week year)."""
    return (day.toordinal() - 1) // 7


def weekly_session(day: date, weekday: int) -> date | None:
    """The session on which a once-a-week strategy trading on ``weekday``
    (0 = Monday) trades in ``day``'s week: that weekday, or the next session
    of the same week when it is a holiday. ``None`` when the rest of the
    week is closed."""
    if not 0 <= weekday <= 4:
        raise ValueError(f"weekday must be 0 (Monday) .. 4 (Friday), got {weekday}")
    candidate = day + timedelta(days=weekday - day.weekday())
    while candidate.weekday() <= 4:
        if is_session(candidate):
            return candidate
        candidate += timedelta(days=1)
    return None


# ---- regular sessions of any exchange (roadmap 21.3.1) ------------------------


@dataclass(frozen=True)
class RegularSession:
    """One regular session: its local date and its open and close as naive
    UTC instants."""

    day: date
    open: datetime
    close: datetime


def _naive_utc(when: datetime) -> datetime:
    if when.tzinfo is None:
        return when
    return when.astimezone(UTC).replace(tzinfo=None)


@lru_cache(maxsize=64)
def _calendar_name(ticker: str) -> str | None:
    from stonks.scheduling.calendar import UnknownCalendarError, calendar_for_ticker

    try:
        return calendar_for_ticker(ticker).name
    except UnknownCalendarError:
        return None


@lru_cache(maxsize=8192)
def _session_on(calendar: str, day: date) -> RegularSession | None:
    from stonks.scheduling.calendar import CalendarRangeError, get_calendar

    try:
        s = get_calendar(calendar).session(day)
    except CalendarRangeError:
        return None
    if s is None:
        return None
    return RegularSession(s.date, _naive_utc(s.open), _naive_utc(s.close))


def regular_session(ticker: str, at: datetime) -> RegularSession | None:
    """The regular session of ``ticker``'s exchange that is open at ``at``
    (naive means UTC), or ``None``: closed, an unknown calendar, or a date
    outside the calendar's range."""
    name = _calendar_name(ticker)
    if name is None:
        return None
    at = _naive_utc(at)
    # a session's local date can differ from its UTC date by one day
    for day in (at.date() - timedelta(days=1), at.date(), at.date() + timedelta(days=1)):
        s = _session_on(name, day)
        if s is not None and s.open <= at < s.close:
            return s
    return None


def previous_regular_session(ticker: str, session: RegularSession) -> RegularSession | None:
    """The regular session before ``session`` on ``ticker``'s exchange, or
    ``None`` when there is none within two weeks."""
    name = _calendar_name(ticker)
    if name is None:
        return None
    day = session.day
    for _ in range(_MAX_BACK_DAYS):
        day -= timedelta(days=1)
        s = _session_on(name, day)
        if s is not None:
            return s
    return None
