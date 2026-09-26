"""Quiet hours: wall-clock windows in the user's time zone, DST-safe."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

from stonks.notify.quiet import QuietHours, parse_hhmm, user_zone


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def test_parse_hhmm():
    assert parse_hhmm("07:30") == time(7, 30)
    for bad in ("7:30", "24:00", "12:60", "noon", "", "07:30:00"):
        with pytest.raises(ValueError):
            parse_hhmm(bad)


def test_same_day_window():
    q = QuietHours(time(12, 0), time(14, 0), "UTC")
    assert not q.is_quiet(_utc(2026, 1, 5, 11, 59))
    assert q.is_quiet(_utc(2026, 1, 5, 12, 0))
    assert q.is_quiet(_utc(2026, 1, 5, 13, 59))
    assert not q.is_quiet(_utc(2026, 1, 5, 14, 0))
    assert q.ends_after(_utc(2026, 1, 5, 13, 0)) == _utc(2026, 1, 5, 14, 0)


def test_window_across_midnight_in_user_zone():
    # 22:00-07:00 in New York (UTC-5 in January).
    q = QuietHours(time(22, 0), time(7, 0), "America/New_York")
    assert not q.is_quiet(_utc(2026, 1, 5, 15, 0))  # 10:00 local
    assert q.is_quiet(_utc(2026, 1, 6, 3, 30))  # 22:30 local
    assert q.is_quiet(_utc(2026, 1, 6, 11, 59))  # 06:59 local
    assert not q.is_quiet(_utc(2026, 1, 6, 12, 0))  # 07:00 local
    # Before midnight local, the window ends tomorrow morning local.
    assert q.ends_after(_utc(2026, 1, 6, 3, 30)) == _utc(2026, 1, 6, 12, 0)
    # After midnight local, it ends this morning.
    assert q.ends_after(_utc(2026, 1, 6, 8, 0)) == _utc(2026, 1, 6, 12, 0)


def test_utc_would_give_the_wrong_answer():
    # 23:00 UTC is 18:00 in New York: quiet in UTC, not there.
    ny = QuietHours(time(22, 0), time(7, 0), "America/New_York")
    utc = QuietHours(time(22, 0), time(7, 0), "UTC")
    now = _utc(2026, 1, 5, 23, 0)
    assert not ny.is_quiet(now)
    assert utc.is_quiet(now)


def test_equal_start_and_end_means_no_quiet_hours():
    q = QuietHours(time(8, 0), time(8, 0), "UTC")
    assert not q.is_quiet(_utc(2026, 1, 5, 8, 0))


def test_dst_spring_forward_end_in_the_gap():
    # US DST starts 2026-03-08 at 02:00 local; 02:30 doesn't exist that day.
    q = QuietHours(time(1, 0), time(2, 30), "America/New_York")
    now = _utc(2026, 3, 8, 6, 30)  # 01:30 EST
    assert q.is_quiet(now)
    end = q.ends_after(now)
    assert end > now
    assert end - now <= timedelta(hours=2)


def test_dst_fall_back_end_is_after_now():
    # US DST ends 2026-11-01 at 02:00 local; 01:00-02:00 happens twice.
    q = QuietHours(time(0, 0), time(1, 30), "America/New_York")
    now = _utc(2026, 11, 1, 5, 10)  # 01:10 EDT (first pass)
    assert q.is_quiet(now)
    end = q.ends_after(now)
    assert end > now
    assert end - now <= timedelta(hours=2)


def test_ends_after_outside_the_window_is_now():
    q = QuietHours(time(22, 0), time(7, 0), "UTC")
    now = _utc(2026, 1, 5, 12, 0)
    assert q.ends_after(now) == now


def test_unknown_zone_falls_back_to_utc():
    assert user_zone("Mars/Olympus_Mons").key == "UTC"
    assert user_zone("").key == "UTC"
    assert user_zone("Europe/Athens").key == "Europe/Athens"
    q = QuietHours(time(22, 0), time(7, 0), "Not/AZone")
    assert q.is_quiet(_utc(2026, 1, 5, 23, 0))
