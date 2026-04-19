"""Integration tests for the extended fundamentals tables (migration 002)
and the DuckDBLake upsert/query methods that wrap them.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd
import pytest

from stonks.store.lake import DuckDBLake


@pytest.fixture
def lake(tmp_path):
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    yield lake
    lake.close()


def test_migration_002_creates_all_extended_tables(lake):
    tables = set(lake.tables())
    expected = {
        "dividends",
        "insider_transactions",
        "news",
        "news_sentiment",
        "analyst_estimates",
        "analyst_ratings",
        "shares_outstanding",
        "employee_count",
        "segmentation",
    }
    assert expected <= tables
    # profile fields added to tickers
    cols = {
        row["column_name"]
        for row in lake.sql(
            "SELECT column_name FROM information_schema.columns WHERE table_name='tickers'"
        ).to_dict(orient="records")
    }
    for expected_col in (
        "name", "country_iso", "fiscal_year_end", "web_url", "is_bank",
        "beta", "short_percent", "employee_count", "esg_score",
    ):
        assert expected_col in cols


def test_upsert_and_read_dividends(lake):
    df = pd.DataFrame(
        [
            {"ticker": "AAPL.US", "ex_date": date(2026, 2, 10), "amount": 0.25,
             "currency": "USD", "pay_date": date(2026, 2, 13),
             "record_date": None, "declaration_date": None},
            {"ticker": "AAPL.US", "ex_date": date(2026, 5, 12), "amount": 0.26,
             "currency": "USD", "pay_date": date(2026, 5, 15),
             "record_date": None, "declaration_date": None},
        ]
    )
    assert lake.upsert_dividends(df) == 2
    out = lake.get_dividends("AAPL.US")
    assert len(out) == 2
    # idempotent
    lake.upsert_dividends(df)
    assert lake.count_rows("dividends") == 2


def test_upsert_insider_transactions_dedupes_by_natural_key(lake):
    df = pd.DataFrame(
        [
            {"ticker": "AAPL.US", "date": date(2026, 3, 1),
             "owner_name": "Cook, Tim", "owner_relation": "Officer",
             "transaction_code": "Sale", "shares": 50_000.0,
             "price": 250.0, "value": 12_500_000.0, "vendor_id": "x1"},
        ]
    )
    lake.upsert_insider_transactions(df)
    lake.upsert_insider_transactions(df)  # idempotent via natural key
    assert lake.count_rows("insider_transactions") == 1


def test_upsert_news_and_query(lake):
    ts = datetime(2026, 4, 1, 14, 30, tzinfo=UTC)
    df = pd.DataFrame(
        [
            {"ticker": "AAPL.US", "published_at": ts,
             "title": "Apple announces new thing",
             "url": "https://example.com/1", "source_name": "Wire",
             "sentiment": 0.5},
        ]
    )
    lake.upsert_news(df)
    lake.upsert_news(df)  # idempotent
    assert lake.count_rows("news") == 1


def test_upsert_news_sentiment(lake):
    df = pd.DataFrame(
        [
            {"ticker": "AAPL.US", "date": date(2026, 4, 1),
             "sentiment": 0.2, "article_count": 12},
            {"ticker": "AAPL.US", "date": date(2026, 4, 2),
             "sentiment": 0.1, "article_count": 8},
        ]
    )
    lake.upsert_news_sentiment(df)
    out = lake.sql("SELECT * FROM news_sentiment WHERE ticker='AAPL.US' ORDER BY date")
    assert len(out) == 2


def test_upsert_analyst_estimates(lake):
    df = pd.DataFrame(
        [
            {"ticker": "AAPL.US", "period_end": date(2025, 12, 31),
             "metric": "epsActual", "value": 2.40},
            {"ticker": "AAPL.US", "period_end": date(2025, 12, 31),
             "metric": "epsEstimate", "value": 2.35},
        ]
    )
    lake.upsert_analyst_estimates(df)
    out = lake.sql("SELECT * FROM analyst_estimates WHERE ticker='AAPL.US'")
    assert len(out) == 2


def test_upsert_analyst_ratings_is_snapshot_update(lake):
    first = pd.DataFrame(
        [
            {"ticker": "AAPL.US", "rating": 2.3, "target_price": 250.0,
             "strong_buy": 10, "buy": 20, "hold": 5, "sell": 2, "strong_sell": 0,
             "updated_at": datetime(2026, 4, 1, tzinfo=UTC)},
        ]
    )
    lake.upsert_analyst_ratings(first)
    second = pd.DataFrame(
        [
            {"ticker": "AAPL.US", "rating": 2.5, "target_price": 260.0,
             "strong_buy": 12, "buy": 20, "hold": 3, "sell": 2, "strong_sell": 0,
             "updated_at": datetime(2026, 4, 15, tzinfo=UTC)},
        ]
    )
    lake.upsert_analyst_ratings(second)
    assert lake.count_rows("analyst_ratings") == 1
    out = lake.sql("SELECT rating, target_price FROM analyst_ratings WHERE ticker='AAPL.US'")
    assert out.iloc[0]["rating"] == 2.5
    assert out.iloc[0]["target_price"] == 260.0


def test_upsert_shares_outstanding(lake):
    df = pd.DataFrame(
        [
            {"ticker": "AAPL.US", "date": date(2025, 9, 30), "shares": 15_600_000_000.0},
            {"ticker": "AAPL.US", "date": date(2024, 9, 30), "shares": 15_800_000_000.0},
        ]
    )
    lake.upsert_shares_outstanding(df)
    assert lake.count_rows("shares_outstanding") == 2


def test_upsert_employee_count(lake):
    df = pd.DataFrame(
        [
            {"ticker": "AAPL.US", "date": date(2025, 9, 30), "count": 164_000},
            {"ticker": "AAPL.US", "date": date(2024, 9, 30), "count": 161_000},
        ]
    )
    lake.upsert_employee_count(df)
    assert lake.count_rows("employee_count") == 2


def test_upsert_segmentation_covers_both_dimensions(lake):
    df = pd.DataFrame(
        [
            {"ticker": "AAPL.US", "period_end": date(2025, 9, 30),
             "dimension": "revenue", "segment": "iPhone", "value": 200e9},
            {"ticker": "AAPL.US", "period_end": date(2025, 9, 30),
             "dimension": "geographic", "segment": "Americas", "value": 160e9},
        ]
    )
    lake.upsert_segmentation(df)
    assert lake.count_rows("segmentation") == 2
    revs = lake.sql(
        "SELECT * FROM segmentation WHERE ticker=? AND dimension='revenue'",
        ["AAPL.US"],
    )
    assert len(revs) == 1


def test_upsert_ticker_profile_inserts_then_updates(lake):
    df = pd.DataFrame(
        [
            {"id": "AAPL.US", "exchange": "US", "currency": "USD",
             "name": "Apple Inc", "country_iso": "US",
             "ipo_date": date(1980, 12, 12), "sector": "Technology",
             "industry": "Consumer Electronics", "fiscal_year_end": "September",
             "web_url": "http://www.apple.com", "is_delisted": False,
             "is_bank": False, "beta": 1.25, "short_percent": 0.006,
             "insider_ownership_percent": 0.07,
             "institutional_ownership_percent": 0.61,
             "employee_count": 164_000, "esg_score": None},
        ]
    )
    lake.upsert_ticker_profile(df)
    row = lake.sql("SELECT * FROM tickers WHERE id='AAPL.US'").iloc[0]
    assert row["name"] == "Apple Inc"
    assert row["beta"] == 1.25

    # update path
    df2 = df.copy()
    df2.loc[0, "beta"] = 1.30
    df2.loc[0, "employee_count"] = 170_000
    lake.upsert_ticker_profile(df2)
    row2 = lake.sql("SELECT beta, employee_count FROM tickers WHERE id='AAPL.US'").iloc[0]
    assert row2["beta"] == 1.30
    assert row2["employee_count"] == 170_000


def test_upsert_empty_dataframes_are_noop(lake):
    empty = pd.DataFrame()
    for fn in (
        lake.upsert_dividends,
        lake.upsert_insider_transactions,
        lake.upsert_news,
        lake.upsert_news_sentiment,
        lake.upsert_analyst_estimates,
        lake.upsert_analyst_ratings,
        lake.upsert_shares_outstanding,
        lake.upsert_employee_count,
        lake.upsert_segmentation,
        lake.upsert_ticker_profile,
    ):
        assert fn(empty) == 0
