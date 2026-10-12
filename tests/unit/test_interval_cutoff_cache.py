"""``visible_cutoff`` is pure, so it is memoised: same answers, cached."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from stonks.core.interval import Interval, _visible_cutoff, cutoff_cache, visible_cutoff


def test_cached_answers_equal_the_plain_function():
    at = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
    cases = [
        (Interval.DAY_1, Interval.DAY_1),
        (Interval.DAY_1, Interval.MIN_5),
        (Interval.DAY_1, None),
        (Interval.MIN_5, None),
        (Interval.HOUR_1, Interval.MIN_5),
        (Interval.parse("1mo"), Interval.DAY_1),
        (Interval.parse("1w"), Interval.DAY_1),
    ]
    for _ in range(2):  # the second pass answers from the cache
        for step in range(5):
            when = at + timedelta(days=step)
            for interval, decision in cases:
                assert visible_cutoff(when, interval, decision) == _visible_cutoff(
                    when, interval, decision
                )


def test_a_repeated_call_hits_the_cache():
    at = datetime(2031, 7, 1, 9, 0, tzinfo=UTC)
    visible_cutoff(at, Interval.DAY_1, Interval.MIN_5)
    hits, misses = cutoff_cache()
    visible_cutoff(at, Interval.DAY_1, Interval.MIN_5)
    assert cutoff_cache() == (hits + 1, misses)


def test_equal_instants_keep_their_own_type_and_zone():
    at = datetime(2032, 1, 5, 15, 0, tzinfo=UTC)
    first = visible_cutoff(at, Interval.DAY_1, Interval.MIN_5)
    stamp = visible_cutoff(pd.Timestamp(at), Interval.DAY_1, Interval.MIN_5)
    assert isinstance(stamp, pd.Timestamp)
    assert not isinstance(first, pd.Timestamp)
    ny = visible_cutoff(at.astimezone(ZoneInfo("America/New_York")), Interval.DAY_1, None)
    assert ny.tzinfo == ZoneInfo("America/New_York")
