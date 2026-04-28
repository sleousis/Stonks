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
    AnalystForecastRow,
    AnalystRatingsRow,
    CrossListingRow,
    DividendRow,
    EarningsAnnouncementRow,
    EmployeeCountRow,
    EsgActivityRow,
    EsgSnapshotRow,
    InsiderTransactionRow,
    InstitutionalHolderRow,
    NewsArticleRow,
    NewsSentimentRow,
    OfficerRow,
    SegmentationRow,
    SharesOutstandingRow,
    TickerProfile,
    TickerSnapshotRow,
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
            id="AAPL.US",
            exchange="US",
            currency="USD",
            name="Apple Inc",
            sector="Technology",
            industry="Consumer Electronics",
            gic_sector="Information Technology",
            cusip="037833100",
            cik="0000320193",
            security_type="common_stock",
        ),
        ticker_snapshot=TickerSnapshotRow(
            ticker="AAPL.US",
            snapshot_date=date(2026, 4, 1),
            beta=1.25,
            short_percent=0.6,
            percent_insiders=7.0,
            percent_institutions=61.0,
        ),
        dividends=(
            DividendRow(ticker="AAPL.US", ex_date=date(2026, 2, 10), amount=0.25, currency="USD"),
        ),
        insider_transactions=(
            InsiderTransactionRow(
                ticker="AAPL.US",
                transaction_date=date(2026, 3, 1),
                filing_date=date(2026, 3, 5),
                owner_name="Cook, Tim",
                owner_cik="0001214156",
                owner_relation="officer",
                owner_title="CEO",
                transaction_code="S",
                acquired_disposed="D",
                shares=50_000.0,
                price=250.0,
                value=12_500_000.0,
                post_transaction_amount=3_200_000.0,
                sec_link="https://sec.gov/form4.xml",
            ),
        ),
        news=(
            NewsArticleRow(
                ticker="AAPL.US",
                published_at=datetime(2026, 4, 1, 14, 30, tzinfo=UTC),
                title="Apple announces new product",
                url="https://example.com/1",
                symbols=("AAPL.US", "MSFT.US"),
                tags=("products", "earnings"),
                sentiment=0.5,
                sentiment_pos=0.5,
                sentiment_neg=0.05,
                sentiment_neu=0.45,
            ),
        ),
        news_sentiment=(
            NewsSentimentRow(
                ticker="AAPL.US", date=date(2026, 4, 1), sentiment=0.2, article_count=12
            ),
        ),
        earnings_announcements=(
            EarningsAnnouncementRow(
                ticker="AAPL.US",
                period_end=date(2025, 12, 31),
                report_date=date(2026, 1, 25),
                before_after_market="after",
                currency="USD",
                eps_actual=2.40,
                eps_estimate=2.35,
                eps_difference=0.05,
                surprise_percent=2.13,
            ),
        ),
        analyst_forecasts=(
            AnalystForecastRow(
                ticker="AAPL.US",
                period_end=date(2026, 3, 31),
                period_relative="current_quarter",
                eps_estimate_avg=1.94,
                eps_estimate_n_analysts=32,
            ),
        ),
        analyst_ratings=(
            AnalystRatingsRow(
                ticker="AAPL.US",
                snapshot_date=date(2026, 4, 1),
                target_price=250.0,
                strong_buy=10,
                buy=20,
                hold=5,
                sell=2,
                strong_sell=0,
            ),
        ),
        institutional_holders=(
            InstitutionalHolderRow(
                ticker="AAPL.US",
                holder_kind="institution",
                name="Vanguard Group Inc",
                snapshot_date=date(2025, 12, 31),
                total_shares_pct=9.7151,
                total_assets_pct=5.6215,
                current_shares=1_426_283_914,
                change_shares=26_856_752,
                change_pct=1.9191,
            ),
            InstitutionalHolderRow(
                ticker="AAPL.US",
                holder_kind="fund",
                name="Vanguard Total Stock Mkt Idx Inv",
                snapshot_date=date(2026, 3, 31),
                total_shares_pct=3.1766,
            ),
        ),
        esg_snapshot=EsgSnapshotRow(
            ticker="AAPL.US",
            rating_date=date(2026, 4, 1),
            total_esg=17.04,
            environment_score=0.7,
            social_score=7.85,
            governance_score=8.49,
            controversy_level=3,
        ),
        esg_activities=(
            EsgActivityRow(
                ticker="AAPL.US", rating_date=date(2026, 4, 1), activity="alcohol", involvement="no"
            ),
        ),
        cross_listings=(
            CrossListingRow(
                ticker="AAPL.US", exchange="LSE", exchange_code="0R2V", name="Apple Inc."
            ),
        ),
        officers=(OfficerRow(ticker="AAPL.US", name="Tim Cook", title="CEO", year_born=1961),),
        shares_outstanding=(
            SharesOutstandingRow(ticker="AAPL.US", date=date(2025, 9, 30), shares=15_600_000_000.0),
        ),
        employee_count=(EmployeeCountRow(ticker="AAPL.US", date=date(2025, 9, 30), count=164_000),),
        segmentation=(
            SegmentationRow(
                ticker="AAPL.US",
                period_end=date(2025, 9, 30),
                dimension="revenue",
                segment="iPhone",
                value=200e9,
            ),
            SegmentationRow(
                ticker="AAPL.US",
                period_end=date(2025, 9, 30),
                dimension="geographic",
                segment="Americas",
                value=160e9,
            ),
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
    assert lake.count_rows("earnings_announcements") == 1
    assert lake.count_rows("analyst_forecasts") == 1
    assert lake.count_rows("analyst_ratings") == 1
    assert lake.count_rows("institutional_holders") == 2
    assert lake.count_rows("esg_snapshots") == 1
    assert lake.count_rows("esg_activities") == 1
    assert lake.count_rows("cross_listings") == 1
    assert lake.count_rows("officers") == 1
    assert lake.count_rows("ticker_snapshots") == 1
    assert lake.count_rows("shares_outstanding") == 1
    assert lake.count_rows("employee_count") == 1
    assert lake.count_rows("segmentation") == 2

    profile = lake.sql("SELECT name, cusip, gic_sector FROM tickers WHERE id='AAPL.US'")
    assert profile.iloc[0]["name"] == "Apple Inc"
    assert profile.iloc[0]["cusip"] == "037833100"
    assert profile.iloc[0]["gic_sector"] == "Information Technology"
    snap = lake.sql("SELECT beta, short_percent FROM ticker_snapshots WHERE ticker='AAPL.US'")
    assert snap.iloc[0]["beta"] == 1.25

    news_row = lake.sql(
        "SELECT symbols, tags, content, sentiment_pos, sentiment_neg, sentiment_neu "
        "FROM news WHERE ticker='AAPL.US'"
    ).iloc[0]
    assert list(news_row["symbols"]) == ["AAPL.US", "MSFT.US"]
    assert list(news_row["tags"]) == ["products", "earnings"]
    assert news_row["content"] is None
    assert news_row["sentiment_pos"] == 0.5
    assert news_row["sentiment_neg"] == 0.05
    assert news_row["sentiment_neu"] == 0.45


def test_run_metadata_is_idempotent(lake):
    src = _FakeMetadataSource({"AAPL.US": _sample_bundle()})
    pipe = IngestPipeline(source=src, lake=lake)
    pipe.run_metadata(["AAPL.US"])
    pipe.run_metadata(["AAPL.US"])  # re-run

    # All these tables stay at the same row count: idempotent upserts +
    # change-detection on the SCD-2 tables.
    assert lake.count_rows("dividends") == 1
    assert lake.count_rows("insider_transactions") == 1
    assert lake.count_rows("analyst_ratings") == 1
    assert lake.count_rows("institutional_holders") == 2
    assert lake.count_rows("ticker_snapshots") == 1
    assert lake.count_rows("esg_snapshots") == 1
    assert lake.count_rows("officers") == 1
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
        "dividends",
        "insider_transactions",
        "news",
        "news_sentiment",
        "earnings_announcements",
        "analyst_forecasts",
        "analyst_ratings",
        "institutional_holders",
        "esg_snapshots",
        "esg_activities",
        "cross_listings",
        "officers",
        "ticker_snapshots",
        "shares_outstanding",
        "employee_count",
        "segmentation",
    ):
        assert lake.count_rows(table) == 0
