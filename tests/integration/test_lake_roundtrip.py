"""Integration tests for DuckDBLake. Uses a temp on-disk DB so we exercise the
actual DuckDB + migration path, not an in-memory shortcut."""

from datetime import date

import pandas as pd
import pytest

from stonks.store.lake import DuckDBLake


@pytest.fixture
def lake(tmp_path):
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    yield lake
    lake.close()


def _prices_df(rows):
    return pd.DataFrame(
        rows,
        columns=["ticker", "date", "open", "high", "low", "close", "adj_close", "volume"],
    )


def _fundamentals_df(rows):
    return pd.DataFrame(
        rows,
        columns=["ticker", "period_end", "frequency", "statement", "line_item", "value"],
    )


def test_migrate_creates_expected_tables(lake):
    tables = set(lake.tables())
    assert {"tickers", "prices", "fundamentals", "ingest_runs", "schema_migrations"} <= tables


def test_migrate_is_idempotent(tmp_path):
    path = tmp_path / "lake.duckdb"
    DuckDBLake(path).migrate()
    DuckDBLake(path).migrate()  # second call must not raise
    with DuckDBLake(path) as lake:
        versions = lake.applied_migrations()
    # Exact set matches the number of .sql files in migrations_duckdb/
    assert versions == sorted(versions)
    assert 1 in versions   # initial schema


def test_upsert_prices_roundtrip(lake):
    df = _prices_df(
        [
            ("AAPL.US", date(2026, 4, 1), 100.0, 110.0, 99.0, 109.0, 109.0, 1_000_000),
            ("AAPL.US", date(2026, 4, 2), 109.0, 112.0, 108.0, 111.0, 111.0, 1_200_000),
        ]
    )
    assert lake.upsert_prices(df) == 2

    read = lake.get_prices("AAPL.US", date(2026, 3, 1), date(2026, 5, 1))
    assert len(read) == 2
    # shim returns pure Python date objects for daily bars
    assert set(read["date"].tolist()) == {date(2026, 4, 1), date(2026, 4, 2)}


def test_upsert_prices_is_idempotent(lake):
    df = _prices_df(
        [("AAPL.US", date(2026, 4, 1), 100.0, 110.0, 99.0, 109.0, 109.0, 1_000_000)]
    )
    lake.upsert_prices(df)
    lake.upsert_prices(df)  # second upsert of same row
    assert lake.count_rows("prices") == 1


def test_upsert_prices_updates_on_conflict(lake):
    pk_row = ("AAPL.US", date(2026, 4, 1))
    lake.upsert_prices(
        _prices_df([pk_row + (100.0, 110.0, 99.0, 109.0, 109.0, 1_000_000)])
    )
    lake.upsert_prices(
        _prices_df([pk_row + (101.0, 115.0, 100.0, 114.0, 114.0, 1_500_000)])
    )
    read = lake.get_prices("AAPL.US", date(2026, 4, 1), date(2026, 4, 1))
    assert read.iloc[0]["close"] == 114.0
    assert read.iloc[0]["volume"] == 1_500_000


def test_upsert_fundamentals_roundtrip(lake):
    df = _fundamentals_df(
        [
            ("AAPL.US", date(2025, 12, 31), "Q", "income", "totalRevenue", 123.0),
            ("AAPL.US", date(2025, 12, 31), "Q", "income", "netIncome", 45.0),
            ("AAPL.US", date(2025, 12, 31), "Q", "balance", "totalAssets", 500.0),
        ]
    )
    assert lake.upsert_fundamentals(df) == 3
    read = lake.get_fundamentals("AAPL.US")
    assert len(read) == 3
    read_income = lake.get_fundamentals("AAPL.US", statement="income")
    assert len(read_income) == 2


def test_upsert_fundamentals_is_idempotent(lake):
    df = _fundamentals_df(
        [("AAPL.US", date(2025, 12, 31), "Q", "income", "totalRevenue", 123.0)]
    )
    lake.upsert_fundamentals(df)
    lake.upsert_fundamentals(df)
    assert lake.count_rows("fundamentals") == 1


def test_ingest_run_lifecycle(lake):
    run_id = lake.open_ingest_run(source="eodhd", kind="prices")
    assert isinstance(run_id, int)
    lake.close_ingest_run(run_id, tickers_ok=3, tickers_failed=1, status="partial")

    runs = lake.sql("SELECT * FROM ingest_runs WHERE id = ?", [run_id])
    assert len(runs) == 1
    assert runs.iloc[0]["source"] == "eodhd"
    assert runs.iloc[0]["kind"] == "prices"
    assert runs.iloc[0]["tickers_ok"] == 3
    assert runs.iloc[0]["tickers_failed"] == 1
    assert runs.iloc[0]["status"] == "partial"
    assert runs.iloc[0]["finished_at"] is not None


def test_upsert_empty_df_is_noop(lake):
    assert lake.upsert_prices(_prices_df([])) == 0
    assert lake.count_rows("prices") == 0


def test_get_prices_returns_empty_df_when_no_match(lake):
    read = lake.get_prices("ZZZZ.US", date(2000, 1, 1), date(2001, 1, 1))
    assert read.empty
    assert list(read.columns) == [
        "ticker",
        "date",
        "open",
        "high",
        "low",
        "close",
        "adj_close",
        "volume",
    ]
