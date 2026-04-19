"""Unit tests for the canonical ingestion row schemas."""

from datetime import date

import pytest
from pydantic import ValidationError

from stonks.ingest.schemas import FundamentalRow, RawPriceBar


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
