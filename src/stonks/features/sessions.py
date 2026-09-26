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
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

from stonks.scheduling.calendar import US_CALENDAR, get_calendar

__all__ = ["is_session", "last_session_of_month", "week_index", "weekly_session"]


@lru_cache(maxsize=256)
def _sessions(year: int) -> frozenset[date] | None:
    """The US session dates of ``year``; ``None`` when the calendar doesn't
    cover the whole year."""
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
