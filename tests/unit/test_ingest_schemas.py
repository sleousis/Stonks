"""Unit tests for the canonical ingestion row schemas."""

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from stonks.ingest.schemas import (
    AnalystForecastRow,
    AnalystRatingsRow,
    CrossListingRow,
    DividendRow,
    EarningsAnnouncementRow,
    EmployeeCountRow,
    EsgActivityRow,
    EsgSnapshotRow,
    FundamentalRow,
    InsiderTransactionRow,
    InstitutionalHolderRow,
    NewsArticleRow,
    NewsSentimentRow,
    OfficerRow,
    RawPriceBar,
    SegmentationRow,
    SharesOutstandingRow,
    TickerProfile,
    TickerSnapshotRow,
)


def test_raw_price_bar_parses_iso_date_string():
    bar = RawPriceBar(
        ticker="AAPL.US",
        date="2026-04-01",  # type: ignore[arg-type]  # pydantic coerces
        open=100.0,
        high=105.0,
        low=99.0,
        close=104.0,
        adj_close=104.0,
        volume=1_000_000,
    )
    assert bar.date == date(2026, 4, 1)
    assert bar.volume == 1_000_000


def test_raw_price_bar_allows_missing_volume():
    bar = RawPriceBar(
        ticker="AAPL.US",
        date=date(2026, 4, 1),
        open=100.0,
        high=105.0,
        low=99.0,
        close=104.0,
        adj_close=104.0,
        volume=None,
    )
    assert bar.volume is None


def test_raw_price_bar_rejects_non_numeric_close():
    with pytest.raises(ValidationError):
        RawPriceBar(
            ticker="AAPL.US",
            date=date(2026, 4, 1),
            open=100.0,
            high=105.0,
            low=99.0,
            close="not-a-number",  # type: ignore[arg-type]
            adj_close=104.0,
            volume=None,
        )


def test_fundamental_row_construction():
    row = FundamentalRow(
        ticker="AAPL.US",
        period_end=date(2025, 12, 31),
        frequency="Q",
        statement="income",
        line_item="totalRevenue",
        value=123_456_789.0,
    )
    assert row.statement == "income"
    assert row.frequency == "Q"
    assert row.value == 123_456_789.0


def test_fundamental_row_rejects_bad_frequency():
    with pytest.raises(ValidationError):
        FundamentalRow(
            ticker="AAPL.US",
            period_end=date(2025, 12, 31),
            frequency="weekly",  # type: ignore[arg-type]
            statement="income",
            line_item="totalRevenue",
            value=1.0,
        )


def test_fundamental_row_rejects_bad_statement():
    with pytest.raises(ValidationError):
        FundamentalRow(
            ticker="AAPL.US",
            period_end=date(2025, 12, 31),
            frequency="Q",
            statement="wrong",  # type: ignore[arg-type]
            line_item="totalRevenue",
            value=1.0,
        )


def test_fundamental_row_allows_null_value():
    row = FundamentalRow(
        ticker="AAPL.US",
        period_end=date(2025, 12, 31),
        frequency="A",
        statement="balance",
        line_item="totalAssets",
        value=None,
    )
    assert row.value is None


# ---- extended fundamentals row schemas -------------------------------------


def test_dividend_row_basic_shape():
    row = DividendRow(
        ticker="AAPL.US",
        ex_date=date(2026, 2, 10),
        amount=0.25,
        currency="USD",
        pay_date=date(2026, 2, 13),
    )
    assert row.currency == "USD"
    assert row.amount == 0.25


def test_dividend_row_rejects_negative_amount():
    with pytest.raises(ValidationError):
        DividendRow(ticker="AAPL.US", ex_date=date(2026, 2, 10), amount=-0.1)


def test_insider_transaction_row_full_shape():
    row = InsiderTransactionRow(
        ticker="AAPL.US",
        transaction_date=date(2026, 3, 1),
        filing_date=date(2026, 3, 5),
        owner_name="Cook, Tim",
        owner_cik="0001214156",
        owner_relation="Chief Executive Officer",
        owner_title="CEO",
        transaction_code="S",
        acquired_disposed="D",
        shares=50_000.0,
        price=250.0,
        value=12_500_000.0,
        post_transaction_amount=3_200_000.0,
        sec_link="https://sec.gov/...",
    )
    assert row.transaction_code == "S"
    assert row.acquired_disposed == "D"
    assert row.filing_date == date(2026, 3, 5)
    assert row.sec_link == "https://sec.gov/..."


def test_insider_transaction_row_rejects_bad_acquired_disposed():
    with pytest.raises(ValidationError):
        InsiderTransactionRow(
            ticker="AAPL.US",
            transaction_date=date(2026, 3, 1),
            acquired_disposed="X",  # type: ignore[arg-type]
        )


def test_news_article_row_accepts_timestamp_and_optional_sentiment():
    row = NewsArticleRow(
        ticker="AAPL.US",
        published_at=datetime(2026, 4, 1, 14, 30, tzinfo=UTC),
        title="Apple announces new product",
        url="https://example.com/story/1",
        source_name="Example Wire",
        sentiment=0.62,
    )
    assert row.sentiment == 0.62


def test_news_article_row_allows_missing_url_and_sentiment():
    row = NewsArticleRow(
        ticker="AAPL.US",
        published_at=datetime(2026, 4, 1, tzinfo=UTC),
        title="t",
    )
    assert row.url is None
    assert row.sentiment is None


def test_news_sentiment_row():
    row = NewsSentimentRow(
        ticker="AAPL.US",
        date=date(2026, 4, 1),
        sentiment=0.2,
        article_count=12,
    )
    assert row.article_count == 12


def test_earnings_announcement_row_basic_shape():
    row = EarningsAnnouncementRow(
        ticker="AAPL.US",
        period_end=date(2025, 12, 31),
        report_date=date(2026, 1, 25),
        before_after_market="after",
        currency="USD",
        eps_actual=2.40,
        eps_estimate=2.35,
        eps_difference=0.05,
        surprise_percent=2.13,
    )
    assert row.before_after_market == "after"
    assert row.eps_actual == 2.40


def test_earnings_announcement_row_rejects_unknown_market_window():
    with pytest.raises(ValidationError):
        EarningsAnnouncementRow(
            ticker="AAPL.US",
            period_end=date(2025, 12, 31),
            before_after_market="BeforeMarket",  # type: ignore[arg-type]
        )


def test_earnings_announcement_row_allows_future_period_with_null_actual():
    """Future quarters carry an estimate but no eps_actual until announced."""
    row = EarningsAnnouncementRow(
        ticker="AAPL.US",
        period_end=date(2026, 6, 30),
        report_date=date(2026, 7, 30),
        eps_estimate=2.10,
    )
    assert row.eps_actual is None
    assert row.eps_estimate == 2.10


def test_analyst_forecast_row_full_shape():
    row = AnalystForecastRow(
        ticker="AAPL.US",
        period_end=date(2026, 3, 31),
        period_relative="current_quarter",
        eps_estimate_avg=1.94,
        eps_estimate_low=1.85,
        eps_estimate_high=2.05,
        eps_estimate_n_analysts=32,
        revenue_estimate_avg=94_000_000_000.0,
        eps_revisions_up_30d=2,
        eps_revisions_down_30d=1,
    )
    assert row.period_relative == "current_quarter"
    assert row.eps_estimate_n_analysts == 32


def test_analyst_forecast_row_rejects_unknown_period():
    with pytest.raises(ValidationError):
        AnalystForecastRow(
            ticker="AAPL.US",
            period_end=date(2026, 3, 31),
            period_relative="0q",  # type: ignore[arg-type]  # vendor string, not canonical
        )


def test_analyst_ratings_row_snapshot():
    row = AnalystRatingsRow(
        ticker="AAPL.US",
        snapshot_date=date(2026, 4, 1),
        rating=2.3,
        target_price=250.0,
        strong_buy=10,
        buy=20,
        hold=5,
        sell=2,
        strong_sell=0,
    )
    assert row.strong_buy == 10
    assert row.sell == 2
    assert row.snapshot_date == date(2026, 4, 1)


def test_institutional_holder_row_distinguishes_kind():
    inst = InstitutionalHolderRow(
        ticker="AAPL.US",
        holder_kind="institution",
        name="Vanguard Group Inc",
        snapshot_date=date(2025, 12, 31),
        total_shares_pct=9.7151,
        current_shares=1_426_283_914,
        change_shares=26_856_752,
        change_pct=1.9191,
    )
    assert inst.holder_kind == "institution"
    fund = InstitutionalHolderRow(
        ticker="AAPL.US",
        holder_kind="fund",
        name="Vanguard Total Stock Mkt Idx Inv",
        snapshot_date=date(2026, 3, 31),
    )
    assert fund.holder_kind == "fund"


def test_institutional_holder_row_rejects_bad_kind():
    with pytest.raises(ValidationError):
        InstitutionalHolderRow(
            ticker="AAPL.US",
            holder_kind="other",  # type: ignore[arg-type]
            name="X",
            snapshot_date=date(2026, 1, 1),
        )


def test_esg_snapshot_row():
    row = EsgSnapshotRow(
        ticker="AAPL.US",
        rating_date=date(2026, 4, 1),
        total_esg=17.04,
        total_esg_percentile=28.5,
        environment_score=0.7,
        social_score=7.85,
        governance_score=8.49,
        controversy_level=3,
    )
    assert row.controversy_level == 3
    assert row.environment_score == 0.7


def test_esg_activity_row():
    row = EsgActivityRow(
        ticker="AAPL.US",
        rating_date=date(2026, 4, 1),
        activity="alcohol",
        involvement="No",
    )
    assert row.activity == "alcohol"
    assert row.involvement == "No"


def test_cross_listing_row():
    row = CrossListingRow(
        ticker="AAPL.US",
        exchange="LSE",
        exchange_code="0R2V",
        name="Apple Inc.",
    )
    assert row.exchange == "LSE"


def test_officer_row():
    row = OfficerRow(
        ticker="AAPL.US",
        name="Mr. Timothy D. Cook",
        title="CEO & Director",
        year_born=1961,
    )
    assert row.year_born == 1961


def test_ticker_snapshot_row():
    row = TickerSnapshotRow(
        ticker="AAPL.US",
        snapshot_date=date(2026, 4, 1),
        beta=1.25,
        short_percent=0.6,
        percent_insiders=7.0,
        percent_institutions=61.0,
    )
    assert row.beta == 1.25
    assert row.percent_institutions == 61.0


def test_shares_outstanding_row_rejects_negative():
    with pytest.raises(ValidationError):
        SharesOutstandingRow(ticker="AAPL.US", date=date(2026, 4, 1), shares=-1.0)


def test_employee_count_row():
    row = EmployeeCountRow(ticker="AAPL.US", date=date(2025, 9, 30), count=164_000)
    assert row.count == 164_000


def test_segmentation_row_revenue_and_geographic_dimensions():
    r = SegmentationRow(
        ticker="AAPL.US",
        period_end=date(2025, 9, 30),
        dimension="revenue",
        segment="iPhone",
        value=200_000_000_000.0,
    )
    g = SegmentationRow(
        ticker="AAPL.US",
        period_end=date(2025, 9, 30),
        dimension="geographic",
        segment="Americas",
        value=160_000_000_000.0,
    )
    assert r.dimension == "revenue"
    assert g.dimension == "geographic"


def test_segmentation_row_rejects_bad_dimension():
    with pytest.raises(ValidationError):
        SegmentationRow(
            ticker="AAPL.US",
            period_end=date(2025, 9, 30),
            dimension="customer",  # type: ignore[arg-type]
            segment="X",
            value=1.0,
        )


def test_ticker_profile_optional_everything_but_id():
    t = TickerProfile(id="AAPL.US")
    assert t.id == "AAPL.US"
    assert t.sector is None
    assert t.is_delisted is False  # default

    full = TickerProfile(
        id="AAPL.US",
        exchange="US",
        currency="USD",
        name="Apple Inc",
        country_iso="US",
        sector="Technology",
        industry="Consumer Electronics",
        gic_sector="Information Technology",
        gic_group="Technology Hardware & Equipment",
        ipo_date=date(1980, 12, 12),
        is_delisted=False,
        delisted_date=None,
        is_bank=False,
        fiscal_year_end="September",
        security_type="common_stock",
        cusip="037833100",
        cik="0000320193",
        isin="US0378331005",
        web_url="http://www.apple.com",
        description="Apple Inc. designs, manufactures, …",
    )
    assert full.cusip == "037833100"
    assert full.security_type == "common_stock"


def test_ticker_profile_rejects_unknown_security_type():
    with pytest.raises(ValidationError):
        TickerProfile(
            id="AAPL.US",
            security_type="Common Stock",  # type: ignore[arg-type]  # raw vendor string
        )
