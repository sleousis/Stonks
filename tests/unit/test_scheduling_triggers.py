"""Calendar-aware triggers (roadmap 12.2): fire times in UTC, DST-safe."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest

from stonks.scheduling.triggers import DailyTrigger, Fire, IntervalTrigger, SessionTrigger


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


# ---- session trigger ---------------------------------------------------------------


def test_after_close_fires_on_trading_days_only():
    trig = SessionTrigger("XNYS", anchor="close", offset=timedelta(minutes=30))
    fires = trig.fires_between(_utc(2026, 11, 24), _utc(2026, 11, 30, 23))
    assert [f.as_of for f in fires] == [
        date(2026, 11, 24),
        date(2026, 11, 25),
        # 26th is Thanksgiving; 27th closes early at 18:00 UTC
        date(2026, 11, 27),
        date(2026, 11, 30),
    ]
    assert fires[2].scheduled_for == _utc(2026, 11, 27, 18, 30)
    assert fires[0].scheduled_for == _utc(2026, 11, 24, 21, 30)  # EST
    assert fires[0].key == "2026-11-24"


def test_after_close_tracks_us_dst():
    trig = SessionTrigger("XNYS", offset=timedelta(minutes=30))
    # Friday before the switch (EST) and Monday after (EDT).
    assert trig.next_fire(_utc(2026, 3, 6)).scheduled_for == _utc(2026, 3, 6, 21, 30)
    assert trig.next_fire(_utc(2026, 3, 7)).scheduled_for == _utc(2026, 3, 9, 20, 30)
    # Autumn: last EDT Friday, then the first EST Monday.
    assert trig.next_fire(_utc(2026, 10, 30)).scheduled_for == _utc(2026, 10, 30, 20, 30)
    assert trig.next_fire(_utc(2026, 10, 31)).scheduled_for == _utc(2026, 11, 2, 21, 30)


def test_window_bounds_are_start_exclusive_end_inclusive():
    trig = SessionTrigger("XNYS", offset=timedelta(minutes=30))
    at = _utc(2026, 9, 25, 20, 30)
    assert trig.fires_between(at, at + timedelta(hours=1)) == []
    assert [f.scheduled_for for f in trig.fires_between(at - timedelta(seconds=1), at)] == [at]


def test_next_fire_is_strictly_after():
    trig = SessionTrigger("XNYS", offset=timedelta(minutes=30))
    at = _utc(2026, 9, 25, 20, 30)
    assert trig.next_fire(at - timedelta(seconds=1)).scheduled_for == at
    assert trig.next_fire(at).scheduled_for == _utc(2026, 9, 28, 20, 30)


def test_offset_past_midnight_keeps_session_date():
    # Tokyo-style: an offset that lands on the next UTC day still belongs
    # to the session it follows.
    trig = SessionTrigger("XNYS", offset=timedelta(hours=5))
    fire = trig.next_fire(_utc(2026, 9, 25, 12))
    assert fire.scheduled_for == _utc(2026, 9, 26, 1)
    assert fire.as_of == date(2026, 9, 25)


def test_open_anchor_with_negative_offset():
    trig = SessionTrigger("XNYS", anchor="open", offset=timedelta(minutes=-15))
    assert trig.next_fire(_utc(2026, 9, 25, 12)).scheduled_for == _utc(2026, 9, 25, 13, 15)


def test_out_of_range_calendar_returns_none():
    trig = SessionTrigger("XNYS")
    assert trig.next_fire(_utc(2090, 1, 1)) is None
    assert trig.fires_between(_utc(2090, 1, 1), _utc(2090, 1, 5)) == []


# ---- daily trigger ------------------------------------------------------------------


def test_daily_local_time_across_dst():
    trig = DailyTrigger(time(18, 0), tz="America/New_York")
    assert trig.next_fire(_utc(2026, 3, 7)).scheduled_for == _utc(2026, 3, 7, 23)
    # 2026-03-08 is the switch day (EDT from 02:00): 18:00 EDT = 22:00 UTC.
    assert trig.next_fire(_utc(2026, 3, 8)).scheduled_for == _utc(2026, 3, 8, 22)


def test_daily_nonexistent_and_ambiguous_local_times_fire_once():
    spring = DailyTrigger(time(2, 30), tz="America/New_York")
    fires = spring.fires_between(_utc(2026, 3, 7, 12), _utc(2026, 3, 9, 12))
    assert [f.as_of for f in fires] == [date(2026, 3, 8), date(2026, 3, 9)]
    autumn = DailyTrigger(time(1, 30), tz="America/New_York")
    fires = autumn.fires_between(_utc(2026, 10, 31, 12), _utc(2026, 11, 2, 12))
    assert [f.as_of for f in fires] == [date(2026, 11, 1), date(2026, 11, 2)]
    # Ambiguous 01:30 resolves to the first (EDT) occurrence.
    assert fires[0].scheduled_for == _utc(2026, 11, 1, 5, 30)


def test_daily_weekdays_and_calendar_filter():
    weekdays = DailyTrigger(time(22, 0), weekdays=frozenset(range(5)))
    fires = weekdays.fires_between(_utc(2026, 9, 25), _utc(2026, 9, 29))
    assert [f.as_of for f in fires] == [date(2026, 9, 25), date(2026, 9, 28)]
    trading = DailyTrigger(time(22, 0), calendar="XNYS")
    fires = trading.fires_between(_utc(2026, 11, 25), _utc(2026, 11, 28))
    assert [f.as_of for f in fires] == [date(2026, 11, 25), date(2026, 11, 27)]


def test_daily_rejects_unknown_timezone():
    with pytest.raises(ValueError, match="timezone"):
        DailyTrigger(time(1, 0), tz="Mars/Olympus")


# ---- interval trigger -------------------------------------------------------------


def test_interval_is_anchored_not_drifting():
    trig = IntervalTrigger(timedelta(hours=4))
    fire = trig.next_fire(_utc(2026, 9, 25, 5, 17))
    assert fire == Fire(_utc(2026, 9, 25, 8), date(2026, 9, 25), "2026-09-25T08:00:00+00:00")
    fires = trig.fires_between(_utc(2026, 9, 25, 8), _utc(2026, 9, 25, 16))
    assert [f.scheduled_for.hour for f in fires] == [12, 16]


def test_interval_must_be_positive():
    with pytest.raises(ValueError):
        IntervalTrigger(timedelta(0))


def test_describe():
    assert "XNYS" in SessionTrigger("XNYS", offset=timedelta(minutes=30)).describe()
    assert "every" in IntervalTrigger(timedelta(minutes=5)).describe()


def test_last_fire_at_or_before():
    lookback = timedelta(days=8)
    session = SessionTrigger("XNYS", offset=timedelta(minutes=30))
    # Saturday: the latest fire is Friday's.
    last = session.last_fire_at_or_before(_utc(2026, 9, 26, 12), lookback)
    assert last.scheduled_for == _utc(2026, 9, 25, 20, 30)
    at = _utc(2026, 9, 25, 20, 30)
    assert session.last_fire_at_or_before(at, lookback).scheduled_for == at
    interval = IntervalTrigger(timedelta(hours=4))
    assert interval.last_fire_at_or_before(_utc(2026, 9, 25, 9), lookback).scheduled_for == _utc(
        2026, 9, 25, 8
    )
    assert interval.last_fire_at_or_before(_utc(2026, 9, 25, 8), lookback).scheduled_for == _utc(
        2026, 9, 25, 8
    )
    assert interval.last_fire_at_or_before(_utc(2026, 9, 25, 9), timedelta(minutes=30)) is None
