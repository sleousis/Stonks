"""Backfill progress in the state database (migration 061)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from stonks.ingest.backfill.state import BackfillState, UnitSpec


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def bstate(state, clock) -> BackfillState:
    return BackfillState(state, clock=clock)


def test_ready_after_migration(bstate):
    assert bstate.ready()


def test_listing_twice_keeps_progress(bstate):
    assert bstate.add_units("daily", [UnitSpec("A.US", "US"), UnitSpec("B.US", "US")]) == 2
    unit = bstate.next_units("daily", 1)[0]
    bstate.mark_done(unit, calls=1, rows=10)
    assert bstate.add_units("daily", [UnitSpec("A.US", "US"), UnitSpec("C.US", "US")]) == 1
    summary = bstate.summary()["daily"]
    assert (summary.done, summary.pending, summary.calls, summary.rows) == (1, 2, 1, 10)
    assert bstate.listed_at("daily") is not None


def test_units_run_by_priority_then_key_and_skip_those_in_flight(bstate):
    bstate.add_units(
        "daily",
        [UnitSpec("Z.US", "US", 0), UnitSpec("A.LSE", "LSE", 1), UnitSpec("B.US", "US", 0)],
    )
    assert [u.key for u in bstate.next_units("daily", 10)] == ["B.US", "Z.US", "A.LSE"]
    assert [u.key for u in bstate.next_units("daily", 10, exclude=["B.US"])] == ["Z.US", "A.LSE"]


def test_the_exchange_filter_keeps_market_wide_units(bstate):
    bstate.add_units(
        "mixed", [UnitSpec("A.US", "US"), UnitSpec("B.LSE", "LSE"), UnitSpec("earnings")]
    )
    keys = [u.key for u in bstate.next_units("mixed", 10, exchanges=["US"])]
    assert keys == ["A.US", "earnings"]


def test_failures_back_off_then_give_up(bstate, clock):
    bstate.add_units("daily", [UnitSpec("A.US", "US")])
    unit = bstate.next_units("daily", 1)[0]
    status = bstate.mark_failure(
        unit, error="boom", calls=1, max_attempts=3, backoff=timedelta(minutes=10)
    )
    assert status == "retry"
    assert bstate.next_units("daily", 1) == []  # waiting
    clock.now += timedelta(minutes=11)
    unit = bstate.next_units("daily", 1)[0]
    assert unit.attempts == 1
    bstate.mark_failure(unit, error="boom", calls=1, max_attempts=3, backoff=timedelta(minutes=10))
    clock.now += timedelta(minutes=19)
    assert bstate.next_units("daily", 1) == []  # the wait doubled to 20 minutes
    clock.now += timedelta(minutes=2)
    unit = bstate.next_units("daily", 1)[0]
    assert (
        bstate.mark_failure(
            unit, error="x" * 900, calls=1, max_attempts=3, backoff=timedelta(minutes=10)
        )
        == "failed"
    )
    summary = bstate.summary()["daily"]
    assert (summary.failed, summary.calls, summary.remaining) == (1, 3, 0)
    assert bstate.retry_failed(["daily"]) == 1
    assert bstate.next_units("daily", 1)[0].attempts == 0


def test_next_retry_at_names_the_earliest_wait(bstate, clock):
    bstate.add_units("daily", [UnitSpec("A.US", "US")])
    unit = bstate.next_units("daily", 1)[0]
    bstate.mark_failure(unit, error="e", calls=0, max_attempts=5, backoff=timedelta(minutes=30))
    assert bstate.next_retry_at(["daily"]) == clock.now + timedelta(minutes=30)
    assert bstate.next_retry_at(["other"]) is None


def test_progress_keeps_the_cursor_and_the_unit_pending(bstate):
    bstate.add_units("intraday_1h", [UnitSpec("A.US", "US")])
    unit = bstate.next_units("intraday_1h", 1)[0]
    bstate.mark_progress(unit, cursor="2021-01-01", calls=5, rows=100)
    again = bstate.next_units("intraday_1h", 1)[0]
    assert again.cursor == "2021-01-01"
    summary = bstate.summary()["intraday_1h"]
    assert (summary.pending, summary.calls, summary.done_calls) == (1, 5, 0)


def test_calls_per_day_and_vendor_readings(bstate, clock):
    day = date(2026, 10, 12)
    bstate.add_calls(day, 40)
    bstate.add_calls(day, 2)
    bstate.record_vendor_usage(day, 500, 100_000, clock.now)
    calls = bstate.calls_on(day)
    assert (calls.calls, calls.vendor_used, calls.vendor_limit) == (42, 500, 100_000)
    assert bstate.calls_on(date(2026, 10, 13)).calls == 0
    assert [c.day for c in bstate.recent_calls(5)] == [day]


def test_done_since_names_the_day_each_unit_finished(bstate):
    bstate.add_units("daily", [UnitSpec("A.US", "US")])
    bstate.mark_done(bstate.next_units("daily", 1)[0], calls=1, rows=1)
    assert bstate.done_since("daily") == {"A.US": date(2026, 10, 12)}
