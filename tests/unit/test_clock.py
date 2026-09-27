"""The clock seam (roadmap 19.1)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from stonks.core.clock import SYSTEM_CLOCK, Clock, FakeClock, iso_now, today


def test_system_clock_is_utc_and_a_clock():
    assert isinstance(SYSTEM_CLOCK, Clock)
    assert SYSTEM_CLOCK.now().tzinfo is UTC


def test_fake_clock_moves_only_when_told():
    clock = FakeClock(datetime(2026, 9, 28, 13, 0))  # naive means UTC
    assert clock.now() == datetime(2026, 9, 28, 13, 0, tzinfo=UTC)
    assert clock.now() == clock.now()
    clock.advance(timedelta(days=1, minutes=5))
    assert clock.now() == datetime(2026, 9, 29, 13, 5, tzinfo=UTC)
    clock.set(datetime(2026, 10, 1, tzinfo=UTC))
    assert today(clock) == date(2026, 10, 1)
    assert iso_now(clock) == "2026-10-01T00:00:00+00:00"
