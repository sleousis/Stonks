"""The bar-visibility rule in ``core.interval`` (RS-03, BL-49).

One source of truth for "which bars and dated rows are known at a decision":
the strategies' bar cache and the point-in-time lake both use it.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from stonks.core.interval import Interval, decision_reach, known_through, visible_cutoff

DAY = datetime(2024, 3, 5)


def test_a_daily_decision_reaches_the_end_of_its_day():
    assert decision_reach(DAY, None) == DAY + timedelta(days=1)
    assert decision_reach(DAY, Interval.DAY_1) == DAY + timedelta(days=1)


def test_a_mid_session_decision_without_an_interval_reaches_itself():
    at = DAY.replace(hour=10)
    assert decision_reach(at, None) == at
    assert decision_reach(at, Interval.HOUR_1) == at + timedelta(hours=1)


def test_visible_cutoff_matches_the_documented_cases():
    # daily decision sees its own day's daily bar
    assert visible_cutoff(DAY, Interval.DAY_1, None) == DAY
    # intraday decision without a decision interval: only closed days
    assert visible_cutoff(DAY.replace(hour=9, minute=30), Interval.DAY_1, None) < DAY
    # intraday reads keep the bar the decision stands on
    at = DAY.replace(hour=10)
    assert visible_cutoff(at, Interval.HOUR_1, None) == at
    # a known 1h decision sees day D only at its 23:00 bar
    assert visible_cutoff(DAY.replace(hour=23), Interval.DAY_1, Interval.HOUR_1) == DAY
    assert visible_cutoff(DAY, Interval.DAY_1, Interval.HOUR_1) < DAY
    # a daily run sees every hourly bar of its day
    assert visible_cutoff(DAY, Interval.HOUR_1, Interval.DAY_1) == DAY.replace(hour=23)


def test_month_bars_use_calendar_months_and_clamp_the_day():
    at = datetime(2024, 3, 31)
    # reach is Apr 1; a monthly bar stamped Mar 1 closes on Apr 1
    assert visible_cutoff(at, Interval.MONTH_1, None) == datetime(2024, 3, 1)
    # reach May 31 minus one month clamps to Apr 30
    assert visible_cutoff(datetime(2024, 5, 30), Interval.MONTH_1, None) == datetime(2024, 4, 30)
    assert visible_cutoff(datetime(2024, 3, 31), Interval.YEAR_1, None) == datetime(2023, 4, 1)


def test_known_through_is_the_last_whole_day_before_the_reach():
    assert known_through(DAY, None) == date(2024, 3, 5)
    assert known_through(DAY.replace(hour=10), None) == date(2024, 3, 4)
    assert known_through(DAY.replace(hour=10), Interval.HOUR_1) == date(2024, 3, 4)
    assert known_through(DAY.replace(hour=23), Interval.HOUR_1) == date(2024, 3, 5)
