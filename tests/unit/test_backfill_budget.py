"""The backfill's daily call budget: vendor counts, ceilings, the run cap,
the UTC reset, pacing and per-thread call tracking."""

from __future__ import annotations

import threading
from datetime import UTC, date, datetime, timedelta

import pytest

from stonks.ingest.backfill.budget import BudgetExhausted, CallBudget, next_reset
from stonks.ingest.sources.base import ApiUsage


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


def _budget(clock: _Clock, usage=None, **kwargs) -> CallBudget:
    kwargs.setdefault("requests_per_minute", 1000)
    kwargs.setdefault("daily_budget", 100)
    kwargs.setdefault("sleep", lambda s: None)
    return CallBudget(clock=clock, usage=usage, **kwargs)


NOON = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def test_next_reset_is_midnight_utc_plus_margin():
    assert next_reset(NOON, timedelta(minutes=10)) == datetime(2026, 10, 13, 0, 10, tzinfo=UTC)


def test_charges_until_the_budget_and_never_past_it():
    budget = _budget(_Clock(NOON), daily_budget=25)
    budget.before_request("/fundamentals/A.US", 10)
    budget.before_request("/fundamentals/B.US", 10)
    with pytest.raises(BudgetExhausted) as info:
        budget.before_request("/fundamentals/C.US", 10)
    assert info.value.resets_at == datetime(2026, 10, 13, 0, 10, tzinfo=UTC)
    assert budget.used_today == 20
    budget.before_request("/eod/A.US", 1)  # a cheap call still fits
    assert budget.remaining == 4


def test_the_vendor_count_includes_other_processes():
    usage = lambda: ApiUsage(used_today=90, daily_limit=100_000, day=date(2026, 10, 12))  # noqa: E731
    budget = _budget(_Clock(NOON), usage=usage, daily_budget=100)
    budget.before_request("/fundamentals/A.US", 10)
    with pytest.raises(BudgetExhausted):
        budget.before_request("/eod/A.US", 1)


def test_the_plan_limit_caps_a_larger_budget():
    usage = lambda: ApiUsage(used_today=0, daily_limit=15, day=None)  # noqa: E731
    budget = _budget(_Clock(NOON), usage=usage, daily_budget=95_000)
    assert budget.remaining == 15


def test_the_vendor_count_is_read_again_after_the_interval():
    clock = _Clock(NOON)
    reads: list[int] = []

    def usage() -> ApiUsage:
        reads.append(1)
        return ApiUsage(used_today=10 * len(reads), daily_limit=None)

    budget = _budget(clock, usage=usage, daily_budget=1000, usage_check_seconds=60)
    budget.before_request("/eod/A.US", 1)
    budget.before_request("/eod/B.US", 1)
    assert len(reads) == 1
    assert budget.used_today == 12
    clock.now += timedelta(seconds=61)
    budget.before_request("/eod/C.US", 1)
    assert len(reads) == 2
    assert budget.used_today == 21  # the new reading plus this call


def test_a_vendor_count_for_yesterday_means_the_counter_reset():
    usage = lambda: ApiUsage(used_today=99_000, daily_limit=100_000, day=date(2026, 10, 11))  # noqa: E731
    budget = _budget(_Clock(NOON), usage=usage, daily_budget=95_000)
    assert budget.remaining == 95_000


def test_a_failed_read_falls_back_to_the_stored_count():
    def usage() -> ApiUsage:
        raise OSError("down")

    budget = _budget(_Clock(NOON), usage=usage, daily_budget=100, spent_today=lambda day: 95)
    with pytest.raises(BudgetExhausted):
        budget.before_request("/fundamentals/A.US", 10)
    budget.before_request("/eod/A.US", 5)


def test_the_run_cap_stops_without_a_reset():
    budget = _budget(_Clock(NOON), daily_budget=1000, run_cap=5)
    budget.before_request("/intraday/A.US", 5)
    with pytest.raises(BudgetExhausted) as info:
        budget.before_request("/eod/A.US", 1)
    assert info.value.resets_at is None
    assert budget.run_spent == 5


def test_free_requests_are_never_refused():
    budget = _budget(_Clock(NOON), daily_budget=1)
    budget.before_request("/eod/A.US", 1)
    budget.before_request("/user", 0)


def test_exhaust_stops_until_the_next_day():
    clock = _Clock(NOON)
    budget = _budget(clock, daily_budget=1000)
    budget.exhaust()
    with pytest.raises(BudgetExhausted):
        budget.before_request("/eod/A.US", 1)
    clock.now = datetime(2026, 10, 13, 1, 0, tzinfo=UTC)
    budget.before_request("/eod/A.US", 1)


def test_a_new_utc_day_starts_from_zero_and_keeps_the_old_day_for_storing():
    clock = _Clock(datetime(2026, 10, 12, 23, 59, tzinfo=UTC))
    budget = _budget(clock, daily_budget=10)
    budget.before_request("/fundamentals/A.US", 10)
    clock.now = datetime(2026, 10, 13, 0, 1, tzinfo=UTC)
    budget.before_request("/fundamentals/B.US", 10)
    flushed = budget.flush()
    assert [(f.day, f.calls) for f in flushed] == [
        (date(2026, 10, 12), 10),
        (date(2026, 10, 13), 10),
    ]
    assert budget.flush() == []


def test_ensure_room_refuses_work_that_does_not_fit():
    budget = _budget(_Clock(NOON), daily_budget=10)
    budget.ensure_room(10)
    with pytest.raises(BudgetExhausted):
        budget.ensure_room(11)


def test_track_counts_only_this_threads_calls():
    budget = _budget(_Clock(NOON), daily_budget=1000)
    seen: dict[str, int] = {}

    def work(name: str, cost: int) -> None:
        with budget.track() as tracker:
            budget.before_request(f"/x/{name}", cost)
            budget.before_request(f"/x/{name}", cost)
        seen[name] = tracker.calls

    threads = [threading.Thread(target=work, args=(n, c)) for n, c in (("a", 1), ("b", 10))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen == {"a": 2, "b": 20}


def test_requests_are_paced_to_the_per_minute_limit():
    ticks = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        ticks[0] += seconds

    budget = CallBudget(
        daily_budget=1000,
        requests_per_minute=60,
        clock=_Clock(NOON),
        monotonic=lambda: ticks[0],
        sleep=sleep,
    )
    for _ in range(3):
        budget.before_request("/eod/A.US", 1)
    assert sleeps == [pytest.approx(1.0), pytest.approx(1.0)]
