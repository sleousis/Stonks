"""Integration tests for the extended fundamentals tables (migrations 002 +
005) and the DuckDBLake upsert/query methods that wrap them.
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


def test_migrations_create_all_extended_tables(lake):
    tables = set(lake.tables())
    expected = {
        "dividends",
        "insider_transactions",
        "news",
        "news_sentiment",
        "analyst_ratings",
        "shares_outstanding",
        "employee_count",
        "segmentation",
        # migration 005 additions
        "ticker_snapshots",
        "institutional_holders",
        "earnings_announcements",
        "analyst_forecasts",
        "esg_snapshots",
        "esg_activities",
        "cross_listings",
        "officers",
    }
    assert expected <= tables
    # superseded tables removed by 005
    assert "analyst_estimates" not in tables

    # New static profile fields added to tickers; volatile ones dropped.
    cols = {
        row["column_name"]
        for row in lake.sql(
            "SELECT column_name FROM information_schema.columns WHERE table_name='tickers'"
        ).to_dict(orient="records")
    }
    for expected_col in (
        "name",
        "country_iso",
        "fiscal_year_end",
        "web_url",
        "is_bank",
        "delisted_date",
        "security_type",
        "cusip",
        "cik",
        "isin",
        "open_figi",
        "lei",
        "gic_sector",
        "gic_group",
        "gic_industry",
        "gic_sub_industry",
        "address_street",
        "address_city",
        "address_country",
        "description",
        "updated_at",
    ):
        assert expected_col in cols, f"missing column {expected_col}"
    for dropped_col in (
        "beta",
        "short_percent",
        "insider_ownership_percent",
        "institutional_ownership_percent",
        "employee_count",
        "esg_score",
    ):
        assert dropped_col not in cols, f"column {dropped_col} should have been dropped"


def test_upsert_and_read_dividends(lake):
    df = pd.DataFrame(
        [
            {
                "ticker": "AAPL.US",
                "ex_date": date(2026, 2, 10),
                "amount": 0.25,
                "currency": "USD",
                "pay_date": date(2026, 2, 13),
                "record_date": None,
                "declaration_date": None,
            },
            {
                "ticker": "AAPL.US",
                "ex_date": date(2026, 5, 12),
                "amount": 0.26,
                "currency": "USD",
                "pay_date": date(2026, 5, 15),
                "record_date": None,
                "declaration_date": None,
            },
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
            {
                "ticker": "AAPL.US",
                "transaction_date": date(2026, 3, 1),
                "filing_date": date(2026, 3, 5),
                "owner_name": "Cook, Tim",
                "owner_cik": "0001214156",
                "owner_relation": "Officer",
                "owner_title": "CEO",
                "transaction_code": "S",
                "acquired_disposed": "D",
                "shares": 50_000.0,
                "price": 250.0,
                "value": 12_500_000.0,
                "post_transaction_amount": 3_200_000.0,
                "sec_link": "https://sec.gov/form4.xml",
            },
        ]
    )
    lake.upsert_insider_transactions(df)
    lake.upsert_insider_transactions(df)  # idempotent via natural key
    assert lake.count_rows("insider_transactions") == 1


def test_upsert_news_and_query(lake):
    ts = datetime(2026, 4, 1, 14, 30, tzinfo=UTC)
    df = pd.DataFrame(
        [
            {
                "ticker": "AAPL.US",
                "published_at": ts,
                "title": "Apple announces new thing",
                "url": "https://example.com/1",
                "source_name": "Wire",
                "content": None,
                "symbols": ["AAPL.US", "MSFT.US"],
                "tags": ["products"],
                "sentiment": 0.5,
                "sentiment_pos": 0.5,
                "sentiment_neg": 0.05,
                "sentiment_neu": 0.45,
            },
        ]
    )
    lake.upsert_news(df)
    lake.upsert_news(df)  # idempotent
    assert lake.count_rows("news") == 1
    out = lake.sql("SELECT symbols, tags, sentiment_pos FROM news WHERE ticker='AAPL.US'")
    assert list(out.iloc[0]["symbols"]) == ["AAPL.US", "MSFT.US"]
    assert list(out.iloc[0]["tags"]) == ["products"]
    assert out.iloc[0]["sentiment_pos"] == 0.5


def test_upsert_news_sentiment(lake):
    df = pd.DataFrame(
        [
            {"ticker": "AAPL.US", "date": date(2026, 4, 1), "sentiment": 0.2, "article_count": 12},
            {"ticker": "AAPL.US", "date": date(2026, 4, 2), "sentiment": 0.1, "article_count": 8},
        ]
    )
    lake.upsert_news_sentiment(df)
    out = lake.sql("SELECT * FROM news_sentiment WHERE ticker='AAPL.US' ORDER BY date")
    assert len(out) == 2


def test_upsert_earnings_announcements(lake):
    df = pd.DataFrame(
        [
            {
                "ticker": "AAPL.US",
                "period_end": date(2025, 12, 31),
                "report_date": date(2026, 1, 25),
                "before_after_market": "after",
                "currency": "USD",
                "eps_actual": 2.40,
                "eps_estimate": 2.35,
                "eps_difference": 0.05,
                "surprise_percent": 2.13,
            },
            {
                "ticker": "AAPL.US",
                "period_end": date(2026, 3, 31),
                "report_date": date(2026, 4, 30),
                "before_after_market": "after",
                "currency": "USD",
                "eps_actual": None,
                "eps_estimate": 1.94,
                "eps_difference": None,
                "surprise_percent": None,
            },
        ]
    )
    lake.upsert_earnings_announcements(df)
    assert lake.count_rows("earnings_announcements") == 2
    # idempotent
    lake.upsert_earnings_announcements(df)
    assert lake.count_rows("earnings_announcements") == 2


def test_upsert_analyst_forecasts(lake):
    df = pd.DataFrame(
        [
            {
                "ticker": "AAPL.US",
                "period_end": date(2026, 3, 31),
                "period_relative": "current_quarter",
                "growth": 0.05,
                "eps_estimate_avg": 1.94,
                "eps_estimate_low": 1.85,
                "eps_estimate_high": 2.05,
                "eps_estimate_year_ago": 1.85,
                "eps_estimate_n_analysts": 32,
                "eps_estimate_growth": 0.0486,
                "revenue_estimate_avg": 94e9,
                "revenue_estimate_low": 92e9,
                "revenue_estimate_high": 96e9,
                "revenue_estimate_year_ago": None,
                "revenue_estimate_n_analysts": 30,
                "revenue_estimate_growth": 0.0322,
                "eps_trend_current": 1.94,
                "eps_trend_7d_ago": 1.94,
                "eps_trend_30d_ago": 1.93,
                "eps_trend_60d_ago": 1.90,
                "eps_trend_90d_ago": 1.88,
                "eps_revisions_up_7d": 0,
                "eps_revisions_up_30d": 2,
                "eps_revisions_down_7d": 0,
                "eps_revisions_down_30d": 1,
            },
        ]
    )
    lake.upsert_analyst_forecasts(df)
    assert lake.count_rows("analyst_forecasts") == 1


def test_upsert_analyst_ratings_is_change_detected_time_series(lake):
    """analyst_ratings is now (ticker, snapshot_date) keyed; identical
    re-fetches don't grow the table, but a real change does."""
    first = pd.DataFrame(
        [
            {
                "ticker": "AAPL.US",
                "snapshot_date": date(2026, 4, 1),
                "target_price": 250.0,
                "strong_buy": 10,
                "buy": 20,
                "hold": 5,
                "sell": 2,
                "strong_sell": 0,
            },
        ]
    )
    assert lake.upsert_analyst_ratings(first) == 1

    # Same date, same values → no growth (ON CONFLICT updates)
    assert lake.upsert_analyst_ratings(first) == 0
    assert lake.count_rows("analyst_ratings") == 1

    # New date, same values → no new row (change-detection skips it)
    same_values_later = first.copy()
    same_values_later["snapshot_date"] = date(2026, 4, 8)
    assert lake.upsert_analyst_ratings(same_values_later) == 0
    assert lake.count_rows("analyst_ratings") == 1

    # New date, real consensus shift → INSERT
    different = first.copy()
    different["snapshot_date"] = date(2026, 4, 15)
    different["target_price"] = 260.0
    different["strong_buy"] = 12
    assert lake.upsert_analyst_ratings(different) == 1
    assert lake.count_rows("analyst_ratings") == 2


def test_upsert_institutional_holders_is_change_detected(lake):
    df = pd.DataFrame(
        [
            {
                "ticker": "AAPL.US",
                "holder_kind": "institution",
                "name": "Vanguard Group Inc",
                "snapshot_date": date(2025, 12, 31),
                "total_shares_pct": 9.7151,
                "total_assets_pct": 5.6215,
                "current_shares": 1_426_283_914,
                "change_shares": 26_856_752,
                "change_pct": 1.9191,
            },
        ]
    )
    assert lake.upsert_institutional_holders(df) == 1
    # Same snapshot, same values: no growth
    assert lake.upsert_institutional_holders(df) == 0
    # Same holder, newer date, identical position: no growth (change-detected)
    same_position = df.copy()
    same_position["snapshot_date"] = date(2026, 1, 31)
    assert lake.upsert_institutional_holders(same_position) == 0
    # Newer date with a position change: insert
    moved = same_position.copy()
    moved["current_shares"] = 1_500_000_000
    moved["change_shares"] = 73_716_086
    assert lake.upsert_institutional_holders(moved) == 1
    assert lake.count_rows("institutional_holders") == 2


def test_upsert_esg_snapshots_and_activities(lake):
    snap = pd.DataFrame(
        [
            {
                "ticker": "AAPL.US",
                "rating_date": date(2026, 4, 1),
                "total_esg": 17.04,
                "total_esg_percentile": 28.5,
                "environment_score": 0.7,
                "environment_percentile": 12.0,
                "social_score": 7.85,
                "social_percentile": 32.0,
                "governance_score": 8.49,
                "governance_percentile": 56.0,
                "controversy_level": 3,
            },
        ]
    )
    assert lake.upsert_esg_snapshots(snap) == 1
    activities = pd.DataFrame(
        [
            {
                "ticker": "AAPL.US",
                "rating_date": date(2026, 4, 1),
                "activity": "alcohol",
                "involvement": "No",
            },
            {
                "ticker": "AAPL.US",
                "rating_date": date(2026, 4, 1),
                "activity": "tobacco",
                "involvement": "No",
            },
        ]
    )
    lake.upsert_esg_activities(activities)
    assert lake.count_rows("esg_activities") == 2


def test_upsert_cross_listings(lake):
    df = pd.DataFrame(
        [
            {"ticker": "AAPL.US", "exchange": "LSE", "exchange_code": "0R2V", "name": "Apple Inc."},
            {
                "ticker": "AAPL.US",
                "exchange": "XETRA",
                "exchange_code": "APC",
                "name": "Apple Inc.",
            },
        ]
    )
    lake.upsert_cross_listings(df)
    assert lake.count_rows("cross_listings") == 2


def test_upsert_officers_replaces_per_ticker_roster(lake):
    """Officers is current-state: each fetch deletes-then-inserts the roster
    so departures show up as removed rows."""
    initial = pd.DataFrame(
        [
            {"ticker": "AAPL.US", "name": "Tim Cook", "title": "CEO", "year_born": 1961},
            {"ticker": "AAPL.US", "name": "Luca Maestri", "title": "CFO", "year_born": 1964},
        ]
    )
    assert lake.upsert_officers(initial) == 2
    # Re-insert same roster: still 2 rows
    lake.upsert_officers(initial)
    assert lake.count_rows("officers") == 2
    # Maestri leaves (only Cook remains in next fetch)
    after = pd.DataFrame(
        [
            {"ticker": "AAPL.US", "name": "Tim Cook", "title": "CEO", "year_born": 1961},
        ]
    )
    lake.upsert_officers(after)
    assert lake.count_rows("officers") == 1


def test_upsert_ticker_snapshots_is_change_detected(lake):
    df = pd.DataFrame(
        [
            {
                "ticker": "AAPL.US",
                "snapshot_date": date(2026, 4, 1),
                "beta": 1.25,
                "short_percent": 0.6,
                "percent_insiders": 7.0,
                "percent_institutions": 61.0,
            },
        ]
    )
    assert lake.upsert_ticker_snapshots(df) == 1
    # Same date, same values: no growth
    assert lake.upsert_ticker_snapshots(df) == 0
    # Newer date with beta drift: insert
    drifted = df.copy()
    drifted["snapshot_date"] = date(2026, 4, 15)
    drifted["beta"] = 1.30
    assert lake.upsert_ticker_snapshots(drifted) == 1
    assert lake.count_rows("ticker_snapshots") == 2


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
            {
                "ticker": "AAPL.US",
                "period_end": date(2025, 9, 30),
                "dimension": "revenue",
                "segment": "iPhone",
                "value": 200e9,
            },
            {
                "ticker": "AAPL.US",
                "period_end": date(2025, 9, 30),
                "dimension": "geographic",
                "segment": "Americas",
                "value": 160e9,
            },
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
            {
                "id": "AAPL.US",
                "exchange": "US",
                "currency": "USD",
                "name": "Apple Inc",
                "country_iso": "US",
                "sector": "Technology",
                "industry": "Consumer Electronics",
                "gic_sector": "Information Technology",
                "gic_group": "Technology Hardware & Equipment",
                "gic_industry": None,
                "gic_sub_industry": None,
                "ipo_date": date(1980, 12, 12),
                "is_delisted": False,
                "delisted_date": None,
                "is_bank": False,
                "fiscal_year_end": "September",
                "security_type": "common_stock",
                "cusip": "037833100",
                "cik": "0000320193",
                "isin": "US0378331005",
                "open_figi": "BBG000B9XRY4",
                "lei": "HWUPKR0MPOU8FGXBT394",
                "employer_id_number": None,
                "primary_ticker": "AAPL.US",
                "address_street": "One Apple Park Way",
                "address_city": "Cupertino",
                "address_state": "CA",
                "address_country": "USA",
                "address_zip": "95014",
                "phone": "408 996 1010",
                "web_url": "http://www.apple.com",
                "description": "Apple Inc. designs, manufactures, …",
                "updated_at": datetime(2026, 4, 20, tzinfo=UTC),
            },
        ]
    )
    lake.upsert_ticker_profile(df)
    row = lake.sql("SELECT * FROM tickers WHERE id='AAPL.US'").iloc[0]
    assert row["name"] == "Apple Inc"
    assert row["cusip"] == "037833100"
    assert row["gic_sector"] == "Information Technology"

    # update path
    df2 = df.copy()
    df2.loc[0, "industry"] = "Computer Hardware"
    lake.upsert_ticker_profile(df2)
    row2 = lake.sql("SELECT industry FROM tickers WHERE id='AAPL.US'").iloc[0]
    assert row2["industry"] == "Computer Hardware"


def test_upsert_empty_dataframes_are_noop(lake):
    empty = pd.DataFrame()
    for fn in (
        lake.upsert_dividends,
        lake.upsert_insider_transactions,
        lake.upsert_news,
        lake.upsert_news_sentiment,
        lake.upsert_earnings_announcements,
        lake.upsert_analyst_forecasts,
        lake.upsert_analyst_ratings,
        lake.upsert_institutional_holders,
        lake.upsert_esg_snapshots,
        lake.upsert_esg_activities,
        lake.upsert_cross_listings,
        lake.upsert_officers,
        lake.upsert_ticker_snapshots,
        lake.upsert_shares_outstanding,
        lake.upsert_employee_count,
        lake.upsert_segmentation,
        lake.upsert_ticker_profile,
    ):
        assert fn(empty) == 0
