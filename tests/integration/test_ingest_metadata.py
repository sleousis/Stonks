"""Integration test for the metadata ingestion path.

A single ``fetch_metadata`` call returns a ``MetadataBundle`` covering
everything-but-prices-and-statements. The pipeline upserts each non-empty
part into the matching lake table.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from stonks.ingest.metadata_bundle import MetadataBundle
from stonks.ingest.pipeline import IngestPipeline
from stonks.ingest.schemas import (
    AnalystEstimateRow,
    AnalystRatingsRow,
    DividendRow,
    EmployeeCountRow,
    InsiderTransactionRow,
    NewsArticleRow,
    NewsSentimentRow,
    SegmentationRow,
    SharesOutstandingRow,
    TickerProfile,
)
from stonks.ingest.sources.base import DataSource
from stonks.store.lake import DuckDBLake


class _FakeMetadataSource(DataSource):
    source_id = "fake"

    def __init__(self, bundles: dict[str, MetadataBundle], fail_on: set[str] | None = None):
        self._bundles = bundles
        self._fail_on = fail_on or set()

    def list_tickers(self, exchange):
        return sorted(self._bundles.keys())

    def fetch_prices(self, ticker, since=None, until=None):
        return []

    def fetch_fundamentals(self, ticker):
        return []

    def fetch_metadata(self, ticker):
        if ticker in self._fail_on:
            raise RuntimeError(f"boom on {ticker}")
        return self._bundles.get(ticker, MetadataBundle())


def _sample_bundle() -> MetadataBundle:
    return MetadataBundle(
        profile=TickerProfile(
            id="AAPL.US", exchange="US", currency="USD", name="Apple Inc",
            sector="Technology", industry="Consumer Electronics",
            beta=1.25, employee_count=164_000,
        ),
        dividends=(
            DividendRow(ticker="AAPL.US", ex_date=date(2026, 2, 10),
                        amount=0.25, currency="USD"),
        ),
        insider_transactions=(
            InsiderTransactionRow(
                ticker="AAPL.US", date=date(2026, 3, 1),
                owner_name="Cook, Tim", owner_relation="Officer",
                transaction_code="Sale", shares=50_000.0, price=250.0,
                value=12_500_000.0,
            ),
        ),
        news=(
            NewsArticleRow(
                ticker="AAPL.US",
                published_at=datetime(2026, 4, 1, 14, 30, tzinfo=UTC),
                title="Apple announces new product",
                url="https://example.com/1",
                sentiment=0.5,
            ),
        ),
        news_sentiment=(
            NewsSentimentRow(ticker="AAPL.US", date=date(2026, 4, 1),
                             sentiment=0.2, article_count=12),
        ),
        analyst_estimates=(
            AnalystEstimateRow(ticker="AAPL.US", period_end=date(2025, 12, 31),
                               metric="epsActual", value=2.40),
            AnalystEstimateRow(ticker="AAPL.US", period_end=date(2025, 12, 31),
                               metric="epsEstimate", value=2.35),
        ),
        analyst_ratings=AnalystRatingsRow(
            ticker="AAPL.US", rating=2.3, target_price=250.0,
            strong_buy=10, buy=20, hold=5, sell=2, strong_sell=0,
        ),
        shares_outstanding=(
            SharesOutstandingRow(ticker="AAPL.US", date=date(2025, 9, 30),
                                 shares=15_600_000_000.0),
        ),
        employee_count=(
            EmployeeCountRow(ticker="AAPL.US", date=date(2025, 9, 30),
                             count=164_000),
        ),
        segmentation=(
            SegmentationRow(ticker="AAPL.US", period_end=date(2025, 9, 30),
                            dimension="revenue", segment="iPhone", value=200e9),
            SegmentationRow(ticker="AAPL.US", period_end=date(2025, 9, 30),
                            dimension="geographic", segment="Americas", value=160e9),
        ),
    )


@pytest.fixture
def lake(tmp_path):
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    yield lake
    lake.close()


def test_run_metadata_populates_every_table(lake):
    src = _FakeMetadataSource({"AAPL.US": _sample_bundle()})
    pipe = IngestPipeline(source=src, lake=lake)

    result = pipe.run_metadata(["AAPL.US"])

    assert result.status == "ok"
    assert result.tickers_ok == 1
    assert lake.count_rows("dividends") == 1
    assert lake.count_rows("insider_transactions") == 1
    assert lake.count_rows("news") == 1
    assert lake.count_rows("news_sentiment") == 1
    assert lake.count_rows("analyst_estimates") == 2
    assert lake.count_rows("analyst_ratings") == 1
    assert lake.count_rows("shares_outstanding") == 1
    assert lake.count_rows("employee_count") == 1
    assert lake.count_rows("segmentation") == 2

    profile = lake.sql("SELECT name, beta, employee_count FROM tickers WHERE id='AAPL.US'")
    assert profile.iloc[0]["name"] == "Apple Inc"
    assert profile.iloc[0]["beta"] == 1.25
    assert profile.iloc[0]["employee_count"] == 164_000


def test_run_metadata_is_idempotent(lake):
    src = _FakeMetadataSource({"AAPL.US": _sample_bundle()})
    pipe = IngestPipeline(source=src, lake=lake)
    pipe.run_metadata(["AAPL.US"])
    pipe.run_metadata(["AAPL.US"])  # re-run
    assert lake.count_rows("dividends") == 1
    assert lake.count_rows("insider_transactions") == 1
    assert lake.count_rows("analyst_ratings") == 1
    assert lake.count_rows("tickers") == 1


def test_run_metadata_soft_fails_on_bad_ticker(lake):
    src = _FakeMetadataSource(
        {"AAPL.US": _sample_bundle(), "BAD.US": MetadataBundle()},
        fail_on={"BAD.US"},
    )
    pipe = IngestPipeline(source=src, lake=lake)
    result = pipe.run_metadata(["AAPL.US", "BAD.US"])
    assert result.status == "partial"
    assert result.tickers_ok == 1
    assert result.tickers_failed == 1
    # good ticker data landed
    assert lake.count_rows("dividends") == 1


def test_run_metadata_empty_bundle_still_counts_as_ok(lake):
    src = _FakeMetadataSource({"EMPTY.US": MetadataBundle()})
    pipe = IngestPipeline(source=src, lake=lake)
    result = pipe.run_metadata(["EMPTY.US"])
    assert result.status == "ok"
    assert result.tickers_ok == 1
    # nothing written
    for table in (
        "dividends", "insider_transactions", "news", "news_sentiment",
        "analyst_estimates", "analyst_ratings", "shares_outstanding",
        "employee_count", "segmentation",
    ):
        assert lake.count_rows(table) == 0
