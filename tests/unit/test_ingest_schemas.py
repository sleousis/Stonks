"""Unit tests for the canonical ingestion row schemas."""

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from stonks.ingest.schemas import (
    AnalystEstimateRow,
    AnalystRatingsRow,
    DividendRow,
    EmployeeCountRow,
    FundamentalRow,
    InsiderTransactionRow,
    NewsArticleRow,
    NewsSentimentRow,
    RawPriceBar,
    SegmentationRow,
    SharesOutstandingRow,
    TickerProfile,
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


def test_insider_transaction_row():
    row = InsiderTransactionRow(
        ticker="AAPL.US",
        date=date(2026, 3, 1),
        owner_name="Cook, Tim",
        owner_relation="Officer",
        transaction_code="Sale",
        shares=50_000.0,
        price=250.0,
        value=12_500_000.0,
    )
    assert row.transaction_code == "Sale"


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


def test_analyst_estimate_row():
    row = AnalystEstimateRow(
        ticker="AAPL.US",
        period_end=date(2025, 12, 31),
        metric="epsActual",
        value=2.40,
    )
    assert row.metric == "epsActual"


def test_analyst_ratings_row_snapshot():
    row = AnalystRatingsRow(
        ticker="AAPL.US",
        rating=2.3,
        target_price=250.0,
        strong_buy=10,
        buy=20,
        hold=5,
        sell=2,
        strong_sell=0,
        updated_at=datetime(2026, 4, 1, tzinfo=UTC),
    )
    assert row.strong_buy == 10
    assert row.sell == 2


def test_shares_outstanding_row_rejects_negative():
    with pytest.raises(ValidationError):
        SharesOutstandingRow(
            ticker="AAPL.US", date=date(2026, 4, 1), shares=-1.0
        )


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
    assert t.is_delisted is False   # default

    full = TickerProfile(
        id="AAPL.US",
        exchange="US",
        currency="USD",
        name="Apple Inc",
        country_iso="US",
        ipo_date=date(1980, 12, 12),
        sector="Technology",
        industry="Consumer Electronics",
        fiscal_year_end="September",
        web_url="http://www.apple.com",
        is_delisted=False,
        is_bank=False,
        beta=1.25,
        short_percent=0.006,
        insider_ownership_percent=0.07,
        institutional_ownership_percent=0.61,
        employee_count=164_000,
        esg_score=None,
    )
    assert full.beta == 1.25
