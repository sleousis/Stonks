"""DataEnsurer: fetch only the missing ranges, concurrently, through one
lake writer (roadmap 10.5). Hermetic: canned sources, no network."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.core.interval import Interval
from stonks.ingest.ensure import (
    DataEnsurer,
    EnsureSettings,
    RateLimiter,
    missing_ranges,
    vendor_limits,
)
from tests.fixtures.universes import FakeListingSource, bars, seed_daily_bars

TODAY = date(2025, 7, 1)


@pytest.fixture
def lake(tmp_path):
    from stonks.store.lake import DuckDBLake

    lk = DuckDBLake(tmp_path / "lake.duckdb")
    lk.migrate()
    yield lk
    lk.close()


def _source(*tickers: str, start=date(2023, 1, 1), end=date(2025, 6, 30), **kw):
    return FakeListingSource(prices={t: bars(t, start, end) for t in tickers}, **kw)


def _ensurer(lake, source, **settings) -> DataEnsurer:
    return DataEnsurer(lake, source, EnsureSettings(**settings), today=TODAY)


# ---- gap arithmetic ----------------------------------------------------------------


def test_missing_ranges_subtracts_covered_spans():
    want = (date(2024, 1, 1), date(2024, 12, 31))
    have = [(date(2024, 3, 1), date(2024, 5, 31)), (date(2024, 5, 15), date(2024, 8, 31))]
    assert missing_ranges(want, have) == [
        (date(2024, 1, 1), date(2024, 2, 29)),
        (date(2024, 9, 1), date(2024, 12, 31)),
    ]
    assert missing_ranges(want, [want]) == []
    assert missing_ranges(want, []) == [want]


def test_rate_limiter_spaces_requests():
    now = [0.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    limiter = RateLimiter(2.0, clock=lambda: now[0], sleep=sleep)
    for _ in range(5):
        limiter.acquire()
    # a burst of one, then one request every half second
    assert now[0] == pytest.approx(2.0)


def test_eodhd_free_tier_limits():
    free = vendor_limits("eodhd", "free")
    assert free.max_history_days == 365 and not free.bulk and not free.intraday
    paid = vendor_limits("eodhd", "all_world")
    assert paid.max_history_days is None and paid.bulk
    assert vendor_limits("fake", None).max_history_days is None


# ---- ensure ---------------------------------------------------------------------------


def test_ensure_fetches_missing_tickers_once(lake):
    source = _source("A.US", "B.US")
    ensurer = _ensurer(lake, source, max_workers=4)
    report = ensurer.ensure(["A.US", "B.US"], date(2024, 1, 1), date(2024, 6, 28))
    assert report.status == "ok"
    assert report.tickers_fetched == 2 and report.tickers_failed == 0
    assert report.run_id is not None
    runs = lake.sql("SELECT kind, tickers_ok, status FROM ingest_runs")
    assert len(runs) == 1 and int(runs.tickers_ok[0]) == 2
    assert lake.count_rows("bars") == 2 * len(bars("A.US", date(2024, 1, 1), date(2024, 6, 28)))
    source.price_calls.clear()

    again = ensurer.ensure(["A.US", "B.US"], date(2024, 1, 1), date(2024, 6, 28))
    assert source.price_calls == []
    assert again.run_id is None and again.tickers_up_to_date == 2
    assert lake.count_rows("ingest_runs") == 1


def test_ensure_fetches_only_the_gap(lake):
    seed_daily_bars(lake, "A.US", date(2024, 1, 1), date(2024, 3, 29))
    source = _source("A.US")
    _ensurer(lake, source).ensure(["A.US"], date(2024, 1, 1), date(2024, 6, 28))
    assert source.price_calls == [("A.US", date(2024, 3, 30), date(2024, 6, 28))]


def test_weekend_only_gaps_are_not_fetched(lake):
    # Friday is the last bar, the window ends on Sunday
    seed_daily_bars(lake, "A.US", date(2024, 6, 3), date(2024, 6, 7))
    source = _source("A.US")
    report = _ensurer(lake, source).ensure(["A.US"], date(2024, 6, 3), date(2024, 6, 9))
    assert source.price_calls == []
    assert report.tickers_up_to_date == 1


def test_failing_ticker_soft_fails(lake):
    source = _source("A.US", "B.US", failing=["B.US"])
    report = _ensurer(lake, source, max_workers=2).ensure(
        ["A.US", "B.US"], date(2024, 1, 1), date(2024, 2, 1)
    )
    assert report.status == "partial"
    assert report.tickers_fetched == 1 and report.tickers_failed == 1
    assert lake.sql("SELECT DISTINCT ticker FROM bars").ticker.tolist() == ["A.US"]


def test_dead_ticker_is_not_fetched_again(lake):
    source = _source("A.US")  # DEAD.US has no data at the vendor
    ensurer = _ensurer(lake, source)
    ensurer.ensure(["DEAD.US"], date(2020, 1, 1), date(2020, 12, 31))
    assert [c[0] for c in source.price_calls] == ["DEAD.US"]
    source.price_calls.clear()
    report = ensurer.ensure(["DEAD.US"], date(2020, 1, 1), date(2020, 12, 31))
    assert source.price_calls == []
    assert report.tickers_up_to_date == 1


def test_recent_empty_ranges_are_retried(lake):
    # the vendor has nothing for the last days yet: ask again next time
    source = _source("A.US", end=date(2025, 6, 20))
    ensurer = _ensurer(lake, source)
    ensurer.ensure(["A.US"], date(2025, 6, 2), date(2025, 6, 30))
    source.price_calls.clear()
    ensurer.ensure(["A.US"], date(2025, 6, 2), date(2025, 6, 30))
    assert source.price_calls == [("A.US", date(2025, 6, 21), date(2025, 6, 30))]


def test_window_is_clipped_to_today(lake):
    source = _source("A.US")
    _ensurer(lake, source).ensure(["A.US"], date(2025, 6, 2), date(2025, 12, 31))
    assert source.price_calls == [("A.US", date(2025, 6, 2), TODAY)]


class _EodhdLike(FakeListingSource):
    source_id = "eodhd"


def test_free_tier_clips_the_start_and_warns(lake):
    source = _EodhdLike(prices={"A.US": bars("A.US", date(2022, 1, 1), date(2025, 6, 30))})
    report = _ensurer(lake, source, plans={"eodhd": "free"}).ensure(
        ["A.US"], date(2022, 1, 1), date(2025, 6, 30)
    )
    ((_, since, _until),) = source.price_calls
    assert since == date(2024, 7, 1)
    assert report.clipped_start == date(2024, 7, 1)
    assert any("365" in w for w in report.warnings)


def test_free_tier_skips_intraday(lake):
    source = _EodhdLike()
    report = _ensurer(lake, source, plans={"eodhd": "free"}).ensure(
        ["A.US"], date(2025, 6, 2), date(2025, 6, 6), interval=Interval.HOUR_1
    )
    assert report.run_id is None
    assert any("intraday" in w for w in report.warnings)


def test_bulk_refreshes_an_exchange_in_one_call_a_day(lake):
    tickers = [f"T{i}.US" for i in range(25)]
    for t in tickers:
        seed_daily_bars(lake, t, date(2025, 6, 2), date(2025, 6, 25))
    source = _source(*tickers, start=date(2025, 6, 2), end=date(2025, 6, 30), bulk=True)
    report = _ensurer(lake, source, bulk=True, bulk_min_tickers=20).ensure(
        tickers, date(2025, 6, 2), date(2025, 6, 30)
    )
    # 26, 27 and 30 June are the missing business days
    assert [d for _, d in sorted(source.bulk_calls)] == [
        date(2025, 6, 26),
        date(2025, 6, 27),
        date(2025, 6, 30),
    ]
    assert source.price_calls == []
    assert report.bulk_days == 3 and report.tickers_fetched == 25
    last = lake.sql("SELECT MAX(timestamp) AS t FROM bars").t[0]
    assert last.date() == date(2025, 6, 30)


def test_bulk_falls_back_per_ticker_when_unsupported(lake):
    tickers = [f"T{i}.US" for i in range(25)]
    for t in tickers:
        seed_daily_bars(lake, t, date(2025, 6, 2), date(2025, 6, 25))
    source = _source(*tickers, start=date(2025, 6, 2), end=date(2025, 6, 30), bulk=False)
    report = _ensurer(lake, source, bulk=True, bulk_min_tickers=20).ensure(
        tickers, date(2025, 6, 2), date(2025, 6, 30)
    )
    assert len(source.price_calls) == 25
    assert report.bulk_days == 0 and report.tickers_fetched == 25


def test_refresh_exchange_day(lake):
    source = _source("A.US", "B.US", start=date(2025, 6, 27), end=date(2025, 6, 27), bulk=True)
    report = _ensurer(lake, source, bulk=True).refresh_exchange_day("US", date(2025, 6, 27))
    assert source.bulk_calls == [("US", date(2025, 6, 27))]
    assert report.tickers_fetched == 2
    assert lake.count_rows("bars") == 2
