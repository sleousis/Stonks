"""US exchange sessions: which calendar days the NYSE / Nasdaq trade.

Calendar-timed strategies (a quarterly rebalance on the last session of a
month, a weekly trade on Wednesdays) need to know a session date *before*
the bar arrives: deciding "today is the last session of May" from the data
would mean peeking at the next bar. The holiday rules are known in advance,
so answering from them is look-ahead free.

Regular full-day closures only: New Year (a Saturday holiday is not moved
to Friday), Martin Luther King Jr., Presidents, Good Friday, Memorial,
Juneteenth (from 2022), Independence, Labor, Thanksgiving and Christmas.
One-off closures (national mourning, weather) are not modelled; a strategy
simply sees no bar that day.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

from pandas.tseries.holiday import (
    AbstractHolidayCalendar,
    GoodFriday,
    Holiday,
    USLaborDay,
    USMartinLutherKingJr,
    USMemorialDay,
    USPresidentsDay,
    USThanksgivingDay,
    nearest_workday,
    sunday_to_monday,
)

__all__ = ["is_session", "last_session_of_month", "week_index", "weekly_session"]


class _USExchangeHolidays(AbstractHolidayCalendar):
    rules = [  # noqa: RUF012 - pandas reads this class attribute
        Holiday("New Year", month=1, day=1, observance=sunday_to_monday),
        USMartinLutherKingJr,
        USPresidentsDay,
        GoodFriday,
        USMemorialDay,
        Holiday("Juneteenth", month=6, day=19, start_date="2022-01-01", observance=nearest_workday),
        Holiday("Independence", month=7, day=4, observance=nearest_workday),
        USLaborDay,
        USThanksgivingDay,
        Holiday("Christmas", month=12, day=25, observance=nearest_workday),
    ]


@lru_cache(maxsize=256)
def _holidays(year: int) -> frozenset[date]:
    days = _USExchangeHolidays().holidays(start=date(year, 1, 1), end=date(year, 12, 31))
    return frozenset(d.date() for d in days)


def is_session(day: date) -> bool:
    """True when the US exchanges hold a regular session on ``day``."""
    return day.weekday() < 5 and day not in _holidays(day.year)


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
