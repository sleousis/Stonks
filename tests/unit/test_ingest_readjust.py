"""Adjustment drift on every daily writer (BE-09, BE-36).

A dividend or split makes the vendor restate ``adj_close`` of every older
bar. A writer that refetches only the last few days must bring the older
stored bars onto the same basis, or momentum and volatility see a fake
jump at the seam."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd
import pytest

from stonks.ingest.ensure import DataEnsurer, EnsureSettings
from stonks.ingest.pipeline import IngestPipeline
from stonks.ingest.schemas import RawPriceBar
from tests.fixtures.universes import FakeListingSource, seed_daily_bars


@pytest.fixture
def lake(tmp_path):
    from stonks.store.lake import DuckDBLake

    lk = DuckDBLake(tmp_path / "lake.duckdb")
    lk.migrate()
    yield lk
    lk.close()


def _bar(day: date, close: float, adj: float) -> RawPriceBar:
    return RawPriceBar(
        ticker="A.US",
        date=day,
        open=close,
        high=close * 1.01,
        low=close * 0.99,
        close=close,
        adj_close=adj,
        volume=1000,
    )


def _days(start: date, end: date) -> list[date]:
    return [d.date() for d in pd.bdate_range(start, end)]


def _adj(lake) -> pd.Series:
    frame = lake.get_prices("A.US", date(2000, 1, 1), date(2100, 1, 1))
    return frame.set_index("date")["adj_close"]


def _clock():
    return datetime(2025, 6, 21, 12, tzinfo=UTC)


def _dividend_source() -> FakeListingSource:
    # a 2% dividend on 06-16: the vendor restates every older bar to 98
    served = [_bar(d, 100.0, 98.0 if d < date(2025, 6, 16) else 100.0) for d in _days(
        date(2025, 6, 2), date(2025, 6, 20)
    )]  # fmt: skip
    return FakeListingSource(prices={"A.US": served})


def test_a_daily_run_over_a_restated_overlap_readjusts_older_bars(lake):
    seed_daily_bars(lake, "A.US", date(2025, 6, 2), date(2025, 6, 13), close=100.0)
    result = IngestPipeline(_dividend_source(), lake, clock=_clock).run_prices(
        ["A.US"], since=date(2025, 6, 11), until=date(2025, 6, 20)
    )
    adj = _adj(lake)
    assert adj[date(2025, 6, 2)] == pytest.approx(98.0)
    assert adj[date(2025, 6, 10)] == pytest.approx(98.0)
    assert adj[date(2025, 6, 13)] == pytest.approx(98.0)
    assert adj[date(2025, 6, 16)] == pytest.approx(100.0)
    assert result.quality is not None
    assert result.quality["readjusted"] == {"A.US": pytest.approx(0.98)}


def test_a_daily_run_with_a_matching_overlap_leaves_older_bars_alone(lake):
    seed_daily_bars(lake, "A.US", date(2025, 6, 2), date(2025, 6, 13), close=100.0)
    served = [_bar(d, 100.0, 100.0) for d in _days(date(2025, 6, 2), date(2025, 6, 20))]
    IngestPipeline(FakeListingSource(prices={"A.US": served}), lake, clock=_clock).run_prices(
        ["A.US"], since=date(2025, 6, 11), until=date(2025, 6, 20)
    )
    assert set(_adj(lake).round(6)) == {100.0}


def test_readjust_can_be_turned_off(lake):
    seed_daily_bars(lake, "A.US", date(2025, 6, 2), date(2025, 6, 13), close=100.0)
    IngestPipeline(
        _dividend_source(), lake, clock=_clock, adjustment_tolerance=None
    ).run_prices(["A.US"], since=date(2025, 6, 11), until=date(2025, 6, 20))
    assert _adj(lake)[date(2025, 6, 2)] == pytest.approx(100.0)


def test_fallback_rows_are_checked_for_drift_too(lake):
    """BE-36: the primary is down for A.US and the fallback serves the gap
    (more than the overlap) on the new basis; the older bars follow."""
    seed_daily_bars(lake, "A.US", date(2025, 6, 2), date(2025, 6, 13), close=100.0)
    primary = FakeListingSource(prices={}, failing=["A.US"])
    fallback = _dividend_source()
    fallback.source_id = "yahoo"

    def factory(src, lk):
        return IngestPipeline(src, lk, fallback=fallback, clock=_clock)

    report = DataEnsurer(
        lake, primary, EnsureSettings(), pipeline_factory=factory, today=date(2025, 6, 21)
    ).ensure(["A.US"], date(2025, 6, 2), date(2025, 6, 20))
    assert report.tickers_fetched == 1
    adj = _adj(lake)
    assert adj[date(2025, 6, 2)] == pytest.approx(98.0)
    assert adj[date(2025, 6, 16)] == pytest.approx(100.0)


def test_a_row_with_null_prices_never_overwrites_a_bar_with_quality_off(lake):
    """BE-37: with the checker off, a vendor row with missing prices is
    dropped (and logged), never stored over a good bar."""
    from stonks.ingest.quality import BarQualityChecker
    from stonks.ingest.quality_config import DataQualityConfig

    seed_daily_bars(lake, "A.US", date(2025, 6, 2), date(2025, 6, 13), close=100.0)
    empty = RawPriceBar(
        ticker="A.US", date=date(2025, 6, 13), open=None, high=None, low=None,
        close=None, adj_close=None, volume=None,
    )  # fmt: skip
    result = IngestPipeline(
        FakeListingSource(prices={"A.US": [empty]}),
        lake,
        quality=BarQualityChecker(DataQualityConfig(enabled=False)),
        clock=_clock,
    ).run_prices(["A.US"], since=date(2025, 6, 13), until=date(2025, 6, 13))
    assert result.status == "ok"
    stored = lake.get_prices("A.US", date(2025, 6, 13), date(2025, 6, 13))
    assert stored["close"].tolist() == [pytest.approx(100.0)]
