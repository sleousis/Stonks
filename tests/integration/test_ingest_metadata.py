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
    BondProfileRow,
    BondYieldRow,
    CommodityContractRow,
    CrossListingRow,
    CryptoProfileRow,
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
from stonks.ingest.sources.base import DataSource, DataSourceError
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
            # DataSourceError is what the pipeline's narrow soft-fail
            # accepts; raising bare Exception would correctly propagate
            # as a programmer-bug signal.
            raise DataSourceError(f"boom on {ticker}")
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

    profile = lake.sql("SELECT name, cusip, gic_sector FROM instruments WHERE id='AAPL.US'")
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
    assert lake.count_rows("instruments") == 1


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


def test_run_metadata_bundle_is_atomic_on_mid_bundle_failure(lake, monkeypatch):
    """C2: if any single ``upsert_*`` call inside ``_upsert_bundle`` raises,
    every write earlier in the bundle must be rolled back. Without this
    transaction guarantee, the lake would end up with profile + dividends +
    splits + insiders persisted but everything after the failure missing —
    then the per-ticker ``except`` would hide the inconsistency."""
    src = _FakeMetadataSource({"AAPL.US": _sample_bundle()})
    pipe = IngestPipeline(source=src, lake=lake)

    # Monkeypatch a mid-bundle method to fail. ``upsert_news_sentiment`` is
    # roughly halfway through ``_upsert_bundle`` so we can verify both that
    # earlier writes (dividends, news) are rolled back AND that later
    # writes (officers) never happen.
    def _boom(_df):
        raise RuntimeError("simulated mid-bundle failure")

    monkeypatch.setattr(lake, "upsert_news_sentiment", _boom)

    # The pipeline narrows soft-fail to known vendor-class exceptions; a
    # bare RuntimeError is treated as a programmer bug and propagates.
    with pytest.raises(RuntimeError, match="simulated mid-bundle failure"):
        pipe.run_metadata(["AAPL.US"])

    # Every metadata table — including those written before the failure
    # point — must be empty: the transaction rolled the bundle back.
    for table in (
        "dividends",
        "news",
        "news_sentiment",
        "earnings_announcements",
        "analyst_ratings",
        "institutional_holders",
        "officers",
    ):
        assert lake.count_rows(table) == 0, f"{table} should be empty after rollback"


def test_run_metadata_propagates_programmer_bugs_instead_of_soft_failing(lake):
    """I1: a TypeError / KeyError / NameError out of a parser is a real
    bug, not "this ticker had no data." The pipeline now narrows its
    per-ticker ``except`` to vendor-class exceptions, so programmer bugs
    surface to the caller (where tests can catch them) rather than being
    silently filed as a failed ticker.
    """

    class _BuggySource(_FakeMetadataSource):
        def fetch_metadata(self, ticker):
            # Simulate a parser bug — exactly the failure mode I1 targets.
            raise TypeError("parser crashed on a vendor field shape")

    src = _BuggySource({})
    pipe = IngestPipeline(source=src, lake=lake)
    with pytest.raises(TypeError, match="parser crashed"):
        pipe.run_metadata(["AAPL.US"])


# ---- multi-asset bundles ---------------------------------------------------


def test_run_metadata_persists_per_class_bundle_fields(lake):
    """A non-equity bundle round-trips through the pipeline into the
    matching per-class table — instruments rows carry the right
    asset_class, equity-only tables stay empty, and bond_yields / supply
    metadata land on the dedicated tables.
    """
    bundles = {
        "BTC-USD.CC": MetadataBundle(
            profile=TickerProfile(id="BTC-USD.CC", asset_class="crypto", name="Bitcoin USD"),
            crypto_profile=CryptoProfileRow(
                ticker="BTC-USD.CC",
                base_symbol="BTC",
                quote_symbol="USD",
                blockchain="Bitcoin",
                consensus_type="proof_of_work",
                circulating_supply=19_700_000.0,
                max_supply=21_000_000.0,
            ),
        ),
        "US10Y.GBOND": MetadataBundle(
            profile=TickerProfile(id="US10Y.GBOND", asset_class="bond"),
            bond_profile=BondProfileRow(
                ticker="US10Y.GBOND",
                issuer_name="United States Treasury",
                issuer_kind="sovereign",
                bond_kind="treasury",
                coupon_rate=4.25,
            ),
            bond_yields=(
                BondYieldRow(
                    ticker="US10Y.GBOND",
                    date=date(2026, 4, 1),
                    yield_to_maturity=4.18,
                    clean_price=99.45,
                ),
            ),
        ),
        "GC.COMM": MetadataBundle(
            profile=TickerProfile(id="GC.COMM", asset_class="commodity"),
            commodity_contract=CommodityContractRow(
                ticker="GC.COMM",
                underlying_symbol="GC",
                contract_kind="continuous",
                contract_size=100.0,
                contract_unit="troy_ounce",
            ),
        ),
    }
    src = _FakeMetadataSource(bundles)
    pipe = IngestPipeline(source=src, lake=lake)
    result = pipe.run_metadata(["BTC-USD.CC", "US10Y.GBOND", "GC.COMM"])

    assert result.status == "ok"
    assert result.tickers_ok == 3

    classes = (
        lake.sql("SELECT id, asset_class FROM instruments ORDER BY id")
        .set_index("id")["asset_class"]
        .to_dict()
    )
    assert classes == {
        "BTC-USD.CC": "crypto",
        "GC.COMM": "commodity",
        "US10Y.GBOND": "bond",
    }

    assert lake.count_rows("crypto_profiles") == 1
    assert lake.count_rows("bond_profiles") == 1
    assert lake.count_rows("commodity_contracts") == 1
    assert lake.count_rows("bond_yield_history") == 1

    # Equity-only tables saw nothing — these bundles never set those fields.
    for equity_only in (
        "dividends",
        "insider_transactions",
        "news",
        "earnings_announcements",
        "analyst_ratings",
        "esg_snapshots",
    ):
        assert lake.count_rows(equity_only) == 0


def _multi_asset_bundles():
    return {
        "BTC-USD.CC": MetadataBundle(
            profile=TickerProfile(id="BTC-USD.CC", asset_class="crypto"),
            crypto_profile=CryptoProfileRow(
                ticker="BTC-USD.CC",
                base_symbol="BTC",
                quote_symbol="USD",
                circulating_supply=19_700_000.0,
            ),
        ),
        "US10Y.GBOND": MetadataBundle(
            profile=TickerProfile(id="US10Y.GBOND", asset_class="bond"),
            bond_profile=BondProfileRow(
                ticker="US10Y.GBOND",
                issuer_kind="sovereign",
                bond_kind="treasury",
            ),
            bond_yields=(
                BondYieldRow(
                    ticker="US10Y.GBOND",
                    date=date(2026, 4, 1),
                    yield_to_maturity=4.18,
                ),
            ),
        ),
        "GC.COMM": MetadataBundle(
            profile=TickerProfile(id="GC.COMM", asset_class="commodity"),
            commodity_contract=CommodityContractRow(
                ticker="GC.COMM",
                contract_kind="continuous",
            ),
        ),
    }


def test_run_metadata_multi_asset_is_idempotent(lake):
    """Re-running the same multi-asset bundles must keep the new
    per-class tables stable. The first three are PK-by-ticker and rely
    on ``ON CONFLICT … DO UPDATE``; ``bond_yield_history`` is composite
    PK and uses the same path. Without this, a regression in any of the
    new ``upsert_*`` SQL would only show up here.
    """
    bundles = _multi_asset_bundles()
    src = _FakeMetadataSource(bundles)
    pipe = IngestPipeline(source=src, lake=lake)
    pipe.run_metadata(list(bundles))
    pipe.run_metadata(list(bundles))  # second run
    assert lake.count_rows("crypto_profiles") == 1
    assert lake.count_rows("bond_profiles") == 1
    assert lake.count_rows("commodity_contracts") == 1
    assert lake.count_rows("bond_yield_history") == 1
    assert lake.count_rows("instruments") == 3


def test_run_metadata_handles_partial_bundle_with_no_per_class_profile(lake):
    """A free-tier-blocked non-equity ticker yields a profile-only
    bundle (no crypto/bond/commodity row). The pipeline must handle
    that without crashing — and the per-class tables must stay empty
    rather than getting a NULL row from a misguided "always upsert"."""
    src = _FakeMetadataSource(
        {
            "BTC-USD.CC": MetadataBundle(
                profile=TickerProfile(id="BTC-USD.CC", asset_class="crypto"),
            ),
        }
    )
    pipe = IngestPipeline(source=src, lake=lake)
    result = pipe.run_metadata(["BTC-USD.CC"])
    assert result.status == "ok"
    assert result.tickers_ok == 1
    assert lake.count_rows("instruments") == 1
    assert lake.count_rows("crypto_profiles") == 0  # no per-class row to upsert
    assert lake.count_rows("bond_profiles") == 0
    assert lake.count_rows("commodity_contracts") == 0
