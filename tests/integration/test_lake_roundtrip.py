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


def test_migrate_creates_expected_tables(lake):
    tables = set(lake.tables())
    # Migration 007 renamed `tickers` → `instruments`; migration 008 split
    # the long-form `fundamentals` table into three wide statement tables.
    # Both old names are intentionally gone — no back-compat view — so the
    # schema surface stays unambiguous.
    assert {
        "instruments",
        "prices",
        "income_statement",
        "balance_sheet",
        "cash_flow_statement",
        "ingest_runs",
        "schema_migrations",
        "crypto_profiles",
        "bond_profiles",
        "bond_yield_history",
        "commodity_contracts",
    } <= tables
    assert "tickers" not in tables
    assert "fundamentals" not in tables


def test_migrate_is_idempotent(tmp_path):
    path = tmp_path / "lake.duckdb"
    DuckDBLake(path).migrate()
    DuckDBLake(path).migrate()  # second call must not raise
    with DuckDBLake(path) as lake:
        versions = lake.applied_migrations()
    # Exact set matches the number of .sql files in migrations_duckdb/
    assert versions == sorted(versions)
    assert 1 in versions  # initial schema


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
    df = _prices_df([("AAPL.US", date(2026, 4, 1), 100.0, 110.0, 99.0, 109.0, 109.0, 1_000_000)])
    lake.upsert_prices(df)
    lake.upsert_prices(df)  # second upsert of same row
    assert lake.count_rows("prices") == 1


def test_upsert_prices_updates_on_conflict(lake):
    pk_row = ("AAPL.US", date(2026, 4, 1))
    lake.upsert_prices(_prices_df([pk_row + (100.0, 110.0, 99.0, 109.0, 109.0, 1_000_000)]))
    lake.upsert_prices(_prices_df([pk_row + (101.0, 115.0, 100.0, 114.0, 114.0, 1_500_000)]))
    read = lake.get_prices("AAPL.US", date(2026, 4, 1), date(2026, 4, 1))
    assert read.iloc[0]["close"] == 114.0
    assert read.iloc[0]["volume"] == 1_500_000


def test_upsert_income_statement_roundtrip(lake):
    df = pd.DataFrame(
        [
            {
                "ticker": "AAPL.US",
                "period_end": date(2025, 12, 31),
                "frequency": "Q",
                "filing_date": date(2026, 1, 25),
                "currency": "USD",
                "revenue": 124_300_000_000.0,
                "cost_of_revenue": 70_000_000_000.0,
                "gross_profit": 54_300_000_000.0,
                "net_income": 36_330_000_000.0,
            },
            {
                "ticker": "AAPL.US",
                "period_end": date(2025, 9, 30),
                "frequency": "Q",
                "filing_date": date(2025, 10, 31),
                "currency": "USD",
                "revenue": 94_900_000_000.0,
                "cost_of_revenue": 52_000_000_000.0,
                "gross_profit": 42_900_000_000.0,
                "net_income": 23_400_000_000.0,
            },
        ]
    )
    assert lake.upsert_income_statement(df) == 2
    read = lake.get_income_statement("AAPL.US")
    assert len(read) == 2
    # DuckDB returns DATE columns as pandas Timestamps; compare via
    # ``.date()`` so the test pins the value, not the surface type.
    assert read.iloc[0]["period_end"].date() == date(2025, 12, 31)
    assert read.iloc[0]["revenue"] == 124_300_000_000.0


def test_upsert_income_statement_is_idempotent_and_updates_on_conflict(lake):
    pk = {
        "ticker": "AAPL.US",
        "period_end": date(2025, 12, 31),
        "frequency": "Q",
    }
    first = pd.DataFrame([{**pk, "revenue": 123.0, "net_income": 45.0}])
    lake.upsert_income_statement(first)
    lake.upsert_income_statement(first)
    assert lake.count_rows("income_statement") == 1

    # Vendor restated revenue: PK is unchanged so the row updates in place.
    revised = pd.DataFrame([{**pk, "revenue": 130.0, "net_income": 45.0}])
    lake.upsert_income_statement(revised)
    assert lake.count_rows("income_statement") == 1
    out = lake.get_income_statement("AAPL.US")
    assert out.iloc[0]["revenue"] == 130.0


def test_upsert_balance_sheet_roundtrip(lake):
    df = pd.DataFrame(
        [
            {
                "ticker": "AAPL.US",
                "period_end": date(2025, 12, 31),
                "frequency": "Q",
                "total_assets": 365_000_000_000.0,
                "total_liabilities": 280_000_000_000.0,
                "total_stockholder_equity": 85_000_000_000.0,
                "cash": 30_000_000_000.0,
            },
        ]
    )
    assert lake.upsert_balance_sheet(df) == 1
    read = lake.get_balance_sheet("AAPL.US")
    assert read.iloc[0]["total_assets"] == 365_000_000_000.0


def test_upsert_cash_flow_statement_roundtrip(lake):
    df = pd.DataFrame(
        [
            {
                "ticker": "AAPL.US",
                "period_end": date(2025, 12, 31),
                "frequency": "Q",
                "operating_cash_flow": 40_000_000_000.0,
                "investing_cash_flow": -5_000_000_000.0,
                "financing_cash_flow": -30_000_000_000.0,
                "free_cash_flow": 35_000_000_000.0,
                "capital_expenditures": -5_000_000_000.0,
            },
        ]
    )
    assert lake.upsert_cash_flow_statement(df) == 1
    read = lake.get_cash_flow_statement("AAPL.US")
    assert read.iloc[0]["free_cash_flow"] == 35_000_000_000.0


def test_get_statement_returns_empty_df_when_no_match(lake):
    assert lake.get_income_statement("ZZZZ.US").empty
    assert lake.get_balance_sheet("ZZZZ.US").empty
    assert lake.get_cash_flow_statement("ZZZZ.US").empty


def test_upsert_statement_empty_df_is_noop(lake):
    empty = pd.DataFrame()
    assert lake.upsert_income_statement(empty) == 0
    assert lake.upsert_balance_sheet(empty) == 0
    assert lake.upsert_cash_flow_statement(empty) == 0


def test_sparse_upsert_preserves_prior_non_null_values(lake):
    """Re-upserting with a sparser DataFrame must NOT wipe prior values
    to NULL — only real (non-NULL) values overwrite. This is the
    contract that makes wide-table upserts safe for callers who don't
    pass every column on every call."""
    pk = {
        "ticker": "AAPL.US",
        "period_end": date(2025, 12, 31),
        "frequency": "Q",
    }
    full = pd.DataFrame([{**pk, "revenue": 100.0, "gross_profit": 60.0, "net_income": 40.0}])
    lake.upsert_income_statement(full)
    sparse = pd.DataFrame([{**pk, "revenue": 110.0}])
    lake.upsert_income_statement(sparse)
    out = lake.get_income_statement("AAPL.US").iloc[0]
    # Real value overwrites:
    assert out["revenue"] == 110.0
    # Absent values are preserved (NOT silently set to NULL):
    assert out["gross_profit"] == 60.0
    assert out["net_income"] == 40.0


def test_upsert_statement_rejects_missing_pk_columns(lake):
    df = pd.DataFrame([{"ticker": "AAPL.US", "revenue": 100.0}])
    with pytest.raises(ValueError, match="missing required PK column"):
        lake.upsert_income_statement(df)


def test_upsert_statement_rejects_null_pk_value(lake):
    df = pd.DataFrame(
        [
            {
                "ticker": "AAPL.US",
                "period_end": None,
                "frequency": "Q",
                "revenue": 100.0,
            }
        ]
    )
    with pytest.raises(ValueError, match="must be non-null"):
        lake.upsert_income_statement(df)


def test_migration_frequency_check_constraint_rejects_bad_value(lake):
    """The CHECK (frequency IN ('Q','A')) constraint guards against a
    bad direct INSERT that the Pydantic Literal would also reject.
    Belt-and-braces: the lake column won't accept what the row type
    won't construct."""
    import duckdb

    with pytest.raises(duckdb.ConstraintException):
        lake.con.execute(
            "INSERT INTO income_statement (ticker, period_end, frequency) "
            "VALUES ('AAPL.US', DATE '2025-12-31', 'weekly')"
        )


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
