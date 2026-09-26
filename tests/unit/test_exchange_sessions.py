"""US exchange session calendar used by calendar-timed strategies."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.features.sessions import (
    is_session,
    last_session_of_month,
    week_index,
    weekly_session,
)


@pytest.mark.parametrize(
    "day",
    [
        date(2024, 1, 1),  # New Year
        date(2024, 1, 15),  # MLK
        date(2024, 2, 19),  # Presidents
        date(2024, 3, 29),  # Good Friday
        date(2021, 5, 31),  # Memorial Day on the 31st
        date(2024, 6, 19),  # Juneteenth
        date(2021, 7, 5),  # July 4 observed on Monday
        date(2024, 9, 2),  # Labor Day
        date(2024, 11, 28),  # Thanksgiving
        date(2022, 12, 26),  # Christmas observed on Monday
        date(2024, 6, 15),  # Saturday
    ],
)
def test_holidays_and_weekends_are_not_sessions(day):
    assert not is_session(day)


def test_ordinary_weekdays_are_sessions():
    assert is_session(date(2024, 6, 18))
    assert is_session(date(2021, 6, 18))  # Juneteenth only from 2022
    assert is_session(date(2021, 12, 31))  # New Year on a Saturday: no Friday close


def test_last_session_of_month_skips_weekends_and_holidays():
    assert last_session_of_month(2024, 2) == date(2024, 2, 29)
    assert last_session_of_month(2021, 5) == date(2021, 5, 28)  # 31st is Memorial Day
    assert last_session_of_month(2024, 8) == date(2024, 8, 30)  # 31st is a Saturday
    assert last_session_of_month(2024, 11) == date(2024, 11, 29)


def test_weekly_session_moves_past_a_holiday_within_the_week():
    # Wednesday 2024-06-19 is Juneteenth: the week's session is Thursday.
    assert weekly_session(date(2024, 6, 17), weekday=2) == date(2024, 6, 20)
    assert weekly_session(date(2024, 6, 21), weekday=2) == date(2024, 6, 20)
    assert weekly_session(date(2024, 6, 12), weekday=2) == date(2024, 6, 12)


def test_weekly_session_is_none_when_the_rest_of_the_week_is_closed():
    # Friday 2024-03-29 is Good Friday: no Friday session that week.
    assert weekly_session(date(2024, 3, 25), weekday=4) is None


def test_week_index_is_monday_anchored_and_continuous():
    monday = date(2024, 12, 30)
    assert week_index(monday) == week_index(date(2025, 1, 5))
    assert week_index(date(2025, 1, 6)) == week_index(monday) + 1
    assert week_index(date(2024, 12, 29)) == week_index(monday) - 1


def test_sessions_come_from_the_market_calendar():
    # One source of truth with the scheduler (scheduling.calendar XNYS):
    # one-off closures the holiday rules never knew are closed too.
    from stonks.scheduling.calendar import get_calendar

    assert not is_session(date(2018, 12, 5))  # national day of mourning
    assert not is_session(date(2025, 1, 9))
    xnys = get_calendar("XNYS")
    for day in (date(2024, 6, 18), date(2024, 6, 19), date(2024, 3, 29), date(2021, 12, 31)):
        assert is_session(day) == xnys.is_trading_day(day)


def test_outside_the_calendar_window_weekdays_count_as_sessions():
    assert is_session(date(1950, 1, 9))  # a Monday before the calendar's first session
    assert not is_session(date(1950, 1, 7))  # a Saturday
