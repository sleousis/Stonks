"""DataEnsurer: fetch only the missing ranges, concurrently, through one
lake writer (roadmap 10.5). Hermetic: canned sources, no network."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd
import pytest

from stonks.core.interval import Interval
from stonks.ingest.ensure import (
    DataEnsurer,
    EnsureSettings,
    RateLimiter,
    missing_ranges,
    vendor_limits,
)
from stonks.ingest.schemas import RawPriceBar
from tests.fixtures.universes import FakeListingSource, bars, seed_daily_bars

TODAY = date(2025, 7, 1)


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
    # the gap starts on 30 March; the 5 stored bars before it are the overlap
    assert source.price_calls == [("A.US", date(2024, 3, 25), date(2024, 6, 28))]
    source.price_calls.clear()
    _ensurer(lake, source, overlap_bars=0).ensure(["A.US"], date(2024, 1, 1), date(2024, 7, 5))
    assert source.price_calls == [("A.US", date(2024, 6, 29), date(2024, 7, 5))]


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
    # the gap starts on 21 June, the overlap on the 5th stored bar before it
    assert source.price_calls == [("A.US", date(2025, 6, 16), date(2025, 6, 30))]


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
    # 26, 27 and 30 June are the missing business days, 25 June the overlap
    assert [d for _, d in sorted(source.bulk_calls)] == [
        date(2025, 6, 25),
        date(2025, 6, 26),
        date(2025, 6, 27),
        date(2025, 6, 30),
    ]
    assert source.price_calls == []
    assert report.bulk_days == 4 and report.tickers_fetched == 25
    last = lake.sql("SELECT MAX(timestamp) AS t FROM bars").t[0]
    assert last.date() == date(2025, 6, 30)


def test_one_long_gap_does_not_turn_bulk_off_for_the_exchange(lake):
    """BE-61: a dead name in a point-in-time universe misses months; it is
    fetched on its own while the others still share the bulk days."""
    tickers = [f"T{i}.US" for i in range(25)]
    for t in tickers:
        seed_daily_bars(lake, t, date(2025, 6, 2), date(2025, 6, 25))
    source = _source(*tickers, "DEAD.US", start=date(2025, 1, 2), end=date(2025, 6, 30), bulk=True)
    report = _ensurer(lake, source, bulk=True, bulk_min_tickers=20).ensure(
        [*tickers, "DEAD.US"], date(2025, 6, 2), date(2025, 6, 30)
    )
    assert report.bulk_days == 4
    assert [c[0] for c in source.price_calls] == ["DEAD.US"]
    assert report.tickers_fetched == 26


@pytest.mark.slow
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


def test_writes_go_through_the_pipeline_factory(lake):
    from stonks.ingest.pipeline import IngestPipeline

    built = []

    def factory(src, lk):
        built.append(src.source_id)
        return IngestPipeline(src, lk)

    source = _source("A.US")
    ensurer = DataEnsurer(lake, source, EnsureSettings(), pipeline_factory=factory, today=TODAY)
    report = ensurer.ensure(["A.US"], date(2024, 1, 1), date(2024, 2, 1))
    assert built == ["fake"] and report.tickers_fetched == 1


# ---- adjustment drift (DS-01) ---------------------------------------------------------


def _bar(ticker, day, close, adj=None, volume=1000):
    return RawPriceBar(
        ticker=ticker,
        date=day,
        open=close,
        high=close * 1.01,
        low=close * 0.99,
        close=close,
        adj_close=close if adj is None else adj,
        volume=volume,
    )


def _days(start, end):
    return [d.date() for d in pd.bdate_range(start, end)]


def _stored(lake, ticker):
    return lake.get_prices(ticker, date(2000, 1, 1), date(2100, 1, 1))


def _put(lake, ticker, day, close, volume=1000):
    lake.upsert_prices(
        pd.DataFrame(
            [
                {
                    "ticker": ticker,
                    "date": day,
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "adj_close": close,
                    "volume": volume,
                }
            ]
        )
    )


def test_a_new_split_refetches_the_whole_adjusted_history(lake):
    # 10 bars stored before a 4:1 split, fetched when adj_close == close
    old = _days(date(2025, 6, 2), date(2025, 6, 13))
    seed_daily_bars(lake, "A.US", old[0], old[-1], close=100.0)
    # after the split the vendor restates the old adj_close (ratio 0.25)
    new = _days(date(2025, 6, 16), date(2025, 6, 20))
    served = [_bar("A.US", d, 100.0, 25.0) for d in old] + [_bar("A.US", d, 25.0) for d in new]
    source = FakeListingSource(prices={"A.US": served})
    report = _ensurer(lake, source, overlap_bars=5).ensure(
        ["A.US"], date(2025, 6, 2), date(2025, 6, 20)
    )
    # one call with a 5-bar overlap, then the full history once drift is seen
    assert source.price_calls == [
        ("A.US", date(2025, 6, 9), date(2025, 6, 20)),
        ("A.US", date(2025, 6, 2), date(2025, 6, 20)),
    ]
    assert report.readjusted == ["A.US"]
    stored = _stored(lake, "A.US")
    assert len(stored) == 15
    assert stored["adj_close"].tolist() == [25.0] * 15


def test_matching_overlap_fetches_only_the_gap_and_overlap(lake):
    seed_daily_bars(lake, "A.US", date(2025, 6, 2), date(2025, 6, 13), close=10.0)
    source = _source("A.US", start=date(2025, 6, 2), end=date(2025, 6, 20))
    report = _ensurer(lake, source, overlap_bars=5).ensure(
        ["A.US"], date(2025, 6, 2), date(2025, 6, 20)
    )
    assert source.price_calls == [("A.US", date(2025, 6, 9), date(2025, 6, 20))]
    assert report.readjusted == []


def test_bars_older_than_the_vendor_limit_are_rescaled(lake):
    # stored since before the free tier's one year: those bars cannot be
    # fetched again, so their adj_close is scaled by the observed factor
    seed_daily_bars(lake, "A.US", date(2024, 6, 3), date(2025, 6, 13), close=100.0)
    kept = _days(date(2024, 7, 1), date(2025, 6, 13))
    new = _days(date(2025, 6, 16), date(2025, 6, 30))
    served = [_bar("A.US", d, 100.0, 99.0) for d in kept] + [_bar("A.US", d, 100.0) for d in new]
    source = _EodhdLike(prices={"A.US": served})
    _ensurer(lake, source, plans={"eodhd": "free"}).ensure(
        ["A.US"], date(2024, 7, 1), date(2025, 6, 30)
    )
    stored = _stored(lake, "A.US").set_index("date")["adj_close"]
    assert stored[date(2024, 6, 3)] == pytest.approx(99.0)
    assert stored[date(2024, 12, 2)] == pytest.approx(99.0)
    assert stored[date(2025, 6, 30)] == pytest.approx(100.0)


def test_a_drifted_bulk_ticker_is_refetched_per_ticker(lake):
    tickers = [f"T{i}.US" for i in range(25)]
    for t in tickers:
        seed_daily_bars(lake, t, date(2025, 6, 2), date(2025, 6, 25))
    prices = {t: bars(t, date(2025, 6, 2), date(2025, 6, 30)) for t in tickers}
    # T0 paid a dividend: the vendor lowered its old adj_close by 2%
    prices["T0.US"] = [
        _bar("T0.US", d, 10.0, 9.8) for d in _days(date(2025, 6, 2), date(2025, 6, 25))
    ] + [_bar("T0.US", d, 10.0) for d in _days(date(2025, 6, 26), date(2025, 6, 30))]
    source = FakeListingSource(prices=prices, bulk=True)
    report = _ensurer(lake, source, bulk=True, bulk_min_tickers=20).ensure(
        tickers, date(2025, 6, 2), date(2025, 6, 30)
    )
    # the last stored day is fetched too, so drift is visible
    assert [d for _, d in sorted(source.bulk_calls)] == [
        date(2025, 6, 25),
        date(2025, 6, 26),
        date(2025, 6, 27),
        date(2025, 6, 30),
    ]
    assert source.price_calls == [("T0.US", date(2025, 6, 2), date(2025, 6, 30))]
    assert report.readjusted == ["T0.US"]
    assert _stored(lake, "T0.US")["adj_close"].iloc[0] == pytest.approx(9.8)


# ---- in-session daily bars (DS-02) ------------------------------------------------------


def _at(*args):
    moment = datetime(*args, tzinfo=UTC)
    return lambda: moment


def test_an_open_session_is_not_fetched_and_is_fetched_after_the_close(lake):
    day = date(2025, 7, 1)  # a Tuesday, the NYSE closes at 20:00 UTC
    seed_daily_bars(lake, "A.US", date(2025, 6, 2), date(2025, 6, 30))
    partial = bars("A.US", date(2025, 6, 2), date(2025, 6, 30)) + [_bar("A.US", day, 11.0)]
    source = FakeListingSource(prices={"A.US": partial})
    ensurer = DataEnsurer(lake, source, EnsureSettings(), clock=_at(2025, 7, 1, 15))
    report = ensurer.ensure(["A.US"], date(2025, 6, 2), day)
    assert source.price_calls == [] and report.tickers_up_to_date == 1
    assert _stored(lake, "A.US")["date"].max() == date(2025, 6, 30)

    final = bars("A.US", date(2025, 6, 2), date(2025, 6, 30)) + [_bar("A.US", day, 12.0)]
    source = FakeListingSource(prices={"A.US": final})
    DataEnsurer(lake, source, EnsureSettings(), clock=_at(2025, 7, 1, 21)).ensure(
        ["A.US"], date(2025, 6, 2), day
    )
    assert _stored(lake, "A.US").set_index("date").loc[day, "close"] == 12.0


def test_an_open_session_bar_in_a_fetch_is_dropped_and_asked_again(lake):
    day = date(2025, 7, 1)
    served = bars("A.US", date(2025, 6, 23), date(2025, 6, 30)) + [_bar("A.US", day, 11.0)]
    source = FakeListingSource(prices={"A.US": served})
    ensurer = DataEnsurer(lake, source, EnsureSettings(), clock=_at(2025, 7, 1, 15))
    ensurer.ensure(["A.US"], date(2025, 6, 23), day)
    assert _stored(lake, "A.US")["date"].max() == date(2025, 6, 30)
    served.append(_bar("A.US", day, 12.0))
    served.remove(served[-2])
    source.price_calls.clear()
    DataEnsurer(lake, source, EnsureSettings(), clock=_at(2025, 7, 1, 21)).ensure(
        ["A.US"], date(2025, 6, 23), day
    )
    assert source.price_calls and source.price_calls[0][2] == day
    assert _stored(lake, "A.US").set_index("date").loc[day, "close"] == 12.0


def test_an_open_session_is_asked_again_with_no_settle_days(lake):
    # settle_days=0 records every fetched range at once: the day whose
    # session was still open must not be recorded as fetched
    day = date(2025, 7, 1)
    served = bars("A.US", date(2025, 6, 23), date(2025, 6, 30)) + [_bar("A.US", day, 11.0)]
    source = FakeListingSource(prices={"A.US": served})
    settings = EnsureSettings(settle_days=0)
    DataEnsurer(lake, source, settings, clock=_at(2025, 7, 1, 15)).ensure(
        ["A.US"], date(2025, 6, 23), day
    )
    assert _stored(lake, "A.US")["date"].max() == date(2025, 6, 30)
    served[-1] = _bar("A.US", day, 12.0)
    DataEnsurer(lake, source, settings, clock=_at(2025, 7, 1, 21)).ensure(
        ["A.US"], date(2025, 6, 23), day
    )
    assert _stored(lake, "A.US").set_index("date").loc[day, "close"] == 12.0


def test_a_partial_bar_stored_by_hand_is_overwritten_by_the_next_fetch(lake):
    # a manual ingest at 11:00 ET stored 1 July with the 11:00 price
    seed_daily_bars(lake, "A.US", date(2025, 6, 2), date(2025, 6, 30))
    _put(lake, "A.US", date(2025, 7, 1), 11.0, volume=10)
    source = FakeListingSource(prices={"A.US": bars("A.US", date(2025, 6, 2), date(2025, 7, 2))})
    DataEnsurer(lake, source, EnsureSettings(), clock=_at(2025, 7, 2, 21)).ensure(
        ["A.US"], date(2025, 6, 2), date(2025, 7, 2)
    )
    stored = _stored(lake, "A.US").set_index("date")
    assert stored.loc[date(2025, 7, 1), "close"] == 10.0
    assert stored.loc[date(2025, 7, 1), "volume"] == 1000


def test_the_pipeline_drops_a_daily_bar_whose_session_is_open(lake):
    from stonks.ingest.pipeline import IngestPipeline

    day = date(2025, 7, 1)
    served = bars("A.US", date(2025, 6, 23), date(2025, 6, 30)) + [_bar("A.US", day, 11.0)]
    source = FakeListingSource(prices={"A.US": served})
    IngestPipeline(source, lake, clock=_at(2025, 7, 1, 15)).run_prices(["A.US"])
    assert _stored(lake, "A.US")["date"].max() == date(2025, 6, 30)


# ---- holidays (DS-08) --------------------------------------------------------------------


def test_a_holiday_only_gap_is_not_fetched(lake):
    # stored up to Thursday 3 July 2025; Friday 4 July is a NYSE holiday
    seed_daily_bars(lake, "A.US", date(2025, 6, 2), date(2025, 7, 3))
    source = _source("A.US")
    ensurer = DataEnsurer(lake, source, EnsureSettings(), today=date(2025, 7, 6))
    plan = ensurer.plan(["A.US"], date(2025, 6, 2), date(2025, 7, 6))
    assert plan.up_to_date == ["A.US"] and plan.gaps == {}
    report = ensurer.ensure(["A.US"], date(2025, 6, 2), date(2025, 7, 6))
    assert source.price_calls == [] and report.run_id is None


# ---- fallback window (DS-05) --------------------------------------------------------------


def test_the_fallback_fetches_only_the_failed_tickers_own_gap(lake):
    from stonks.ingest.pipeline import IngestPipeline

    seed_daily_bars(lake, "A.US", date(2024, 7, 1), date(2025, 6, 27))
    primary = _source("B.US", failing=["A.US"])
    fallback = _source("A.US")
    fallback.source_id = "yahoo"

    def factory(src, lk):
        return IngestPipeline(src, lk, fallback=fallback)

    ensurer = DataEnsurer(lake, primary, EnsureSettings(), pipeline_factory=factory, today=TODAY)
    report = ensurer.ensure(["A.US", "B.US"], date(2024, 7, 1), date(2025, 6, 30))
    assert report.tickers_fetched == 2
    # the gap plus the 5-bar overlap, so its adjustment basis is checked (BE-36)
    assert fallback.price_calls == [("A.US", date(2025, 6, 23), date(2025, 6, 30))]
    # BE-35: a ticker the fallback rescued is not reported as failed
    assert report.tickers_failed == 0
    assert report.failed == []


def test_failed_lists_the_pipelines_failures():
    """BE-35: ``failed`` comes from the pipeline's per-ticker outcomes."""
    from stonks.ingest.pipeline import IngestPipeline
    from stonks.store.lake import DuckDBLake

    lake = DuckDBLake(":memory:")
    lake.migrate()
    try:
        primary = _source("B.US", failing=["A.US"])
        result = IngestPipeline(primary, lake).run_prices(
            ["A.US", "B.US"], since=date(2025, 6, 2), until=date(2025, 6, 6)
        )
        assert result.failed == ("A.US",)
        report = _ensurer(lake, primary).ensure(
            ["A.US", "C.US"], date(2025, 6, 2), date(2025, 6, 6)
        )
        assert report.failed == ["A.US"]
    finally:
        lake.close()


# ---- edge cases ------------------------------------------------------------------------------


def test_a_duplicate_ticker_is_fetched_once(lake):
    source = _source("A.US")
    report = _ensurer(lake, source).ensure(["A.US", "A.US"], date(2024, 1, 1), date(2024, 2, 1))
    assert [c[0] for c in source.price_calls] == ["A.US"]
    assert report.tickers_requested == 1


def test_mixed_crypto_and_equity_over_a_weekend(lake):
    # both stored up to Friday 27 June; the window ends on Sunday 29 June
    lake.upsert_instrument_profile(pd.DataFrame([{"id": "BTC-USD.CC", "asset_class": "crypto"}]))
    seed_daily_bars(lake, "A.US", date(2025, 6, 2), date(2025, 6, 27))
    for d in pd.date_range(date(2025, 6, 2), date(2025, 6, 27)):
        _put(lake, "BTC-USD.CC", d.date(), 1.0)
    ensurer = DataEnsurer(lake, _source("A.US"), EnsureSettings(), today=date(2025, 6, 29))
    plan = ensurer.plan(["A.US", "BTC-USD.CC"], date(2025, 6, 2), date(2025, 6, 29))
    assert plan.up_to_date == ["A.US"]
    assert plan.gaps == {"BTC-USD.CC": [(date(2025, 6, 28), date(2025, 6, 29))]}


def test_a_dead_ticker_is_not_asked_again_after_settle_days(lake):
    source = _source("A.US")  # DEAD.US answers empty
    ensurer = DataEnsurer(lake, source, EnsureSettings(settle_days=14), today=date(2025, 6, 30))
    ensurer.ensure(["DEAD.US"], date(2025, 6, 2), date(2025, 6, 30))
    # the settled part is recorded: only the last 14 days are asked again
    source.price_calls.clear()
    ensurer.ensure(["DEAD.US"], date(2025, 6, 2), date(2025, 6, 30))
    assert source.price_calls == [("DEAD.US", date(2025, 6, 17), date(2025, 6, 30))]
    # once the whole range has settled it is never asked again
    later = DataEnsurer(lake, source, EnsureSettings(settle_days=14), today=date(2025, 7, 31))
    later.ensure(["DEAD.US"], date(2025, 6, 2), date(2025, 6, 30))
    source.price_calls.clear()
    later.ensure(["DEAD.US"], date(2025, 6, 2), date(2025, 6, 30))
    assert source.price_calls == []


# ---- intraday coverage by timestamp (BE-23) --------------------------------------------


class _HourlySource(FakeListingSource):
    """Hourly XNYS-like bars 14:00-20:00 UTC on 2025-06-19 and 06-20, served
    only once complete at ``upto`` (naive UTC)."""

    def __init__(self, upto: datetime) -> None:
        super().__init__()
        self.upto = upto
        self.intraday_calls: list[tuple[date | None, date | None]] = []

    def fetch_intraday_bars(self, ticker, interval, since=None, until=None):
        from datetime import timedelta

        from stonks.ingest.schemas import IntradayBar

        self.intraday_calls.append((since, until))
        out = []
        for day in (date(2025, 6, 19), date(2025, 6, 20)):
            if (since and day < since) or (until and day > until):
                continue
            for hour in range(14, 20):
                ts = datetime(day.year, day.month, day.day, hour)
                if ts + timedelta(hours=1) <= self.upto:
                    out.append(
                        IntradayBar(ticker=ticker, timestamp=ts, open=10, high=10.1, low=9.9,
                                    close=10, adj_close=10, volume=5)
                    )  # fmt: skip
        return out


def test_a_mid_session_intraday_ensure_leaves_no_hole():
    from stonks.store.lake import DuckDBLake

    lake = DuckDBLake(":memory:")
    lake.migrate()
    try:
        hourly = Interval.parse("1h")
        paid = {"fake": "all_in_one"}
        mid = datetime(2025, 6, 20, 17, 30, tzinfo=UTC)
        first = _HourlySource(mid.replace(tzinfo=None))
        DataEnsurer(lake, first, EnsureSettings(plans=paid), clock=lambda: mid).ensure(
            ["A.US"], date(2025, 6, 19), date(2025, 6, 20), hourly
        )
        later = datetime(2025, 6, 23, 12, tzinfo=UTC)
        second = _HourlySource(later.replace(tzinfo=None))
        DataEnsurer(lake, second, EnsureSettings(plans=paid), clock=lambda: later).ensure(
            ["A.US"], date(2025, 6, 19), date(2025, 6, 20), hourly
        )
        assert second.intraday_calls == [(date(2025, 6, 20), date(2025, 6, 20))]
        stored = lake.get_bars("A.US", hourly, datetime(2025, 6, 20), datetime(2025, 6, 21))
        assert pd.to_datetime(stored["timestamp"]).dt.hour.tolist() == list(range(14, 20))
    finally:
        lake.close()


def test_a_coarser_than_daily_interval_is_refused_with_a_warning(lake):
    # vendors serve daily and native intraday bars only: a weekly ensure
    # must not ask the intraday endpoint (EODHD raises ValueError there)
    from stonks.ingest.sources.eodhd import EodhdDataSource

    source = EodhdDataSource(api_key="test-key")
    report = DataEnsurer(lake, source, EnsureSettings(), today=TODAY).ensure(
        ["A.US"], date(2025, 6, 2), date(2025, 6, 30), Interval.WEEK_1
    )
    assert report.run_id is None and report.gaps == 0
    assert any("1w" in w for w in report.warnings)
