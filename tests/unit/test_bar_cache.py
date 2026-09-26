"""Read-through, look-ahead-safe bar cache shared by bar-reading strategies."""

from __future__ import annotations

import gc
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.store.lake import DuckDBLake
from stonks.strategies._common import BarCache, LakeBarCaches, get_last_n_bars
from stonks.strategies.examples.donchian_breakout import DonchianBreakout
from stonks.strategies.examples.trendline_breakout import TrendlineBreakoutStrategy


def _hourly(ticker: str, days: pd.DatetimeIndex, seed: int = 0) -> pd.DataFrame:
    ts = [datetime(d.year, d.month, d.day, h) for d in days for h in range(14, 21)]
    closes = 100.0 + np.cumsum(np.random.default_rng(seed).normal(0, 0.5, len(ts)))
    return pd.DataFrame(
        {
            "ticker": ticker,
            "timestamp": ts,
            "open": closes,
            "high": closes + 0.2,
            "low": closes - 0.2,
            "close": closes,
            "adj_close": closes,
            "volume": 1_000,
        }
    )


@pytest.fixture
def lake(tmp_path):
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    lake.upsert_bars(_hourly("X.US", pd.bdate_range("2026-01-05", "2026-02-27")), Interval.HOUR_1)
    yield lake
    lake.close()


class CountingLake:
    """Delegating lake proxy that counts ``get_bars`` calls."""

    def __init__(self, inner):
        self._inner = inner
        self.calls = 0

    def get_bars(self, *args, **kwargs):
        self.calls += 1
        return self._inner.get_bars(*args, **kwargs)


AS_OFS = [
    datetime(2026, 1, 5, 13),  # before the first bar
    datetime(2026, 1, 5, 14),  # exactly the first bar
    datetime(2026, 1, 20, 16, 30),
    datetime(2026, 2, 2, 15),  # Monday morning, after a weekend
    datetime(2026, 2, 7, 12),  # Saturday
    date(2026, 2, 10),  # plain date = midnight
    datetime(2026, 3, 30),  # after the last bar
]


@pytest.mark.parametrize("as_of", AS_OFS)
@pytest.mark.parametrize("n", [1, 7, 30, 10_000])
def test_last_n_bars_matches_the_query_helper(lake, as_of, n):
    expected = get_last_n_bars(lake, "X.US", Interval.HOUR_1, as_of, n)
    got = BarCache(lake).last_n_bars("X.US", Interval.HOUR_1, as_of, n)
    pd.testing.assert_frame_equal(got, expected, check_dtype=False)


def test_never_returns_bars_after_as_of_even_when_later_ones_are_cached(lake):
    cache = BarCache(lake)
    cache.last_n_bars("X.US", Interval.HOUR_1, datetime(2026, 3, 30), 50)  # warm with all
    as_of = datetime(2026, 1, 20, 16, 30)
    df = cache.last_n_bars("X.US", Interval.HOUR_1, as_of, 10_000)
    assert pd.to_datetime(df["timestamp"]).max() == pd.Timestamp("2026-01-20 16:00")
    closes = cache.last_n_closes("X.US", Interval.HOUR_1, as_of, 10_000)
    assert len(closes) == len(df)
    window = cache.bars_between("X.US", Interval.HOUR_1, datetime(2026, 1, 1), as_of)
    assert pd.to_datetime(window["timestamp"]).max() <= pd.Timestamp(as_of)


def test_reads_the_lake_once_per_ticker_and_interval(lake):
    counting = CountingLake(lake)
    cache = BarCache(counting)
    for as_of in AS_OFS:
        cache.last_n_closes("X.US", Interval.HOUR_1, as_of, 20)
        cache.last_n_bars("X.US", Interval.HOUR_1, as_of, 5)
    assert counting.calls == 1
    cache.last_n_closes("X.US", Interval.DAY_1, AS_OFS[-1], 5)
    cache.last_n_closes("Y.US", Interval.HOUR_1, AS_OFS[-1], 5)
    assert counting.calls == 3


def test_last_n_closes_is_a_float_array_of_the_close_column(lake):
    cache = BarCache(lake)
    as_of = datetime(2026, 2, 2, 15)
    closes = cache.last_n_closes("X.US", Interval.HOUR_1, as_of, 12)
    expected = get_last_n_bars(lake, "X.US", Interval.HOUR_1, as_of, 12)["close"].to_numpy(float)
    assert closes.dtype == np.float64
    np.testing.assert_array_equal(closes, expected)


def test_bars_between_is_inclusive_on_both_ends(lake):
    cache = BarCache(lake)
    start, end = datetime(2026, 1, 6, 15), datetime(2026, 1, 7, 15)
    got = cache.bars_between("X.US", Interval.HOUR_1, start, end)
    expected = lake.get_bars("X.US", Interval.HOUR_1, start=start, end=end)
    pd.testing.assert_frame_equal(got, expected, check_dtype=False)


def test_unknown_ticker_yields_empty_results(lake):
    cache = BarCache(lake)
    assert cache.last_n_bars("NOPE.US", Interval.HOUR_1, AS_OFS[-1], 5).empty
    assert len(cache.last_n_closes("NOPE.US", Interval.HOUR_1, AS_OFS[-1], 5)) == 0
    assert cache.bars_between("NOPE.US", Interval.HOUR_1, AS_OFS[0], AS_OFS[-1]).empty


def test_non_positive_n_yields_empty(lake):
    cache = BarCache(lake)
    assert cache.last_n_bars("X.US", Interval.HOUR_1, AS_OFS[-1], 0).empty
    assert len(cache.last_n_closes("X.US", Interval.HOUR_1, AS_OFS[-1], 0)) == 0


def test_returned_frames_are_copies_callers_cannot_poison_the_cache(lake):
    cache = BarCache(lake)
    as_of = datetime(2026, 2, 2, 15)
    df = cache.last_n_bars("X.US", Interval.HOUR_1, as_of, 5)
    df.loc[:, "close"] = -1.0
    closes = cache.last_n_closes("X.US", Interval.HOUR_1, as_of, 5)
    closes[:] = -2.0
    assert (cache.last_n_closes("X.US", Interval.HOUR_1, as_of, 5) > 0).all()


# ---- per-lake holder ------------------------------------------------------


def test_lake_bar_caches_gives_one_cache_per_lake(lake, tmp_path):
    caches = LakeBarCaches()
    assert caches.for_lake(lake) is caches.for_lake(lake)
    other = DuckDBLake(tmp_path / "other.duckdb")
    try:
        assert caches.for_lake(other) is not caches.for_lake(lake)
    finally:
        other.close()


def test_lake_bar_caches_drop_entries_when_the_lake_goes_away():
    class Lake:  # weak-referenceable stand-in
        def get_bars(self, *a, **k):
            return pd.DataFrame()

    caches = LakeBarCaches()
    tmp = Lake()
    caches.for_lake(tmp)
    assert len(caches) == 1
    del tmp
    gc.collect()
    assert len(caches) == 0


def test_lake_bar_caches_fall_back_to_uncached_reads_for_unweakrefable_lakes(lake):
    class Slotted:
        __slots__ = ("inner", "calls")

        def __init__(self, inner):
            self.inner = inner
            self.calls = 0

        def get_bars(self, *a, **k):
            self.calls += 1
            return self.inner.get_bars(*a, **k)

    slotted = Slotted(lake)
    caches = LakeBarCaches()
    caches.for_lake(slotted).last_n_closes("X.US", Interval.HOUR_1, AS_OFS[-1], 5)
    caches.for_lake(slotted).last_n_closes("X.US", Interval.HOUR_1, AS_OFS[-1], 5)
    assert slotted.calls == 2  # correct, just not cached


# ---- strategies read through the cache --------------------------------------


@pytest.mark.parametrize(
    "make",
    [
        lambda: DonchianBreakout({"ticker": "X.US", "interval": "1h", "lookback": 10}),
        lambda: TrendlineBreakoutStrategy({"ticker": "X.US", "interval": "1h", "lookback": 20}),
    ],
    ids=["donchian", "trendline"],
)
def test_bar_strategies_read_each_series_once_per_instance(lake, make):
    counting = CountingLake(lake)
    strategy = make()
    for as_of in AS_OFS:
        strategy.estimate_return("X.US", as_of, counting)
        strategy.extract_features("X.US", as_of, counting)
    assert counting.calls == 1
