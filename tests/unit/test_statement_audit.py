"""Statement audit (BL-36): set-based accounting checks write statement_flags."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from stonks.store.audit import CHECKS, audit_statements
from stonks.store.lake import DuckDBLake


@pytest.fixture
def lake(tmp_path):
    lk = DuckDBLake(tmp_path / "lake.duckdb")
    lk.migrate()
    yield lk
    lk.close()


def _clean(lake: DuckDBLake, ticker: str = "GOOD.US", period_end=date(2024, 12, 31)) -> None:
    """One annual period plus its four quarters, all internally consistent."""
    key = {"ticker": ticker, "period_end": period_end, "frequency": "A"}
    lake.upsert_income_statement(
        pd.DataFrame(
            [
                {
                    **key,
                    "filing_date": date(2025, 2, 1),
                    "revenue": 400.0,
                    "cost_of_revenue": 160.0,
                    "gross_profit": 240.0,
                    "net_income": 80.0,
                }
            ]
        )
    )
    quarters = [date(2024, 3, 31), date(2024, 6, 30), date(2024, 9, 30), date(2024, 12, 31)]
    lake.upsert_income_statement(
        pd.DataFrame(
            [
                {
                    "ticker": ticker,
                    "period_end": q,
                    "frequency": "Q",
                    "filing_date": date(q.year + (q.month == 12), (q.month % 12) + 1, 15),
                    "revenue": 100.0,
                    "cost_of_revenue": 40.0,
                    "gross_profit": 60.0,
                    "net_income": 20.0,
                }
                for q in quarters
            ]
        )
    )
    lake.upsert_balance_sheet(
        pd.DataFrame(
            [
                {
                    **key,
                    "filing_date": date(2025, 2, 1),
                    "total_assets": 1000.0,
                    "total_liabilities": 600.0,
                    "total_stockholder_equity": 390.0,
                    "noncontrolling_interest": 10.0,
                    "cash_and_equivalents": 50.0,
                    "common_stock_shares_outstanding": 100.0,
                }
            ]
        )
    )
    lake.upsert_cash_flow_statement(
        pd.DataFrame(
            [
                {
                    **key,
                    "filing_date": date(2025, 2, 1),
                    "net_income": 80.0,
                    "end_period_cash_flow": 50.0,
                }
            ]
        )
    )


def _flags(lake: DuckDBLake) -> set[tuple[str, str]]:
    df = lake.get_statement_flags()
    return {(r.ticker, r.check_id) for r in df.itertuples(index=False)}


def test_clean_data_produces_no_flags(lake):
    _clean(lake)
    report = audit_statements(lake)
    assert report.n_flags == 0
    assert _flags(lake) == set()


def test_balance_sheet_identity(lake):
    _clean(lake)
    lake.upsert_balance_sheet(
        pd.DataFrame(
            [
                {
                    "ticker": "GOOD.US",
                    "period_end": date(2024, 12, 31),
                    "frequency": "A",
                    "total_liabilities": 900.0,
                }
            ]
        )
    )
    audit_statements(lake)
    assert _flags(lake) == {("GOOD.US", "balance_identity")}
    row = lake.get_statement_flags("GOOD.US").iloc[0]
    assert row["severity"] == "error"
    assert "total_assets" in row["detail"]


@pytest.mark.parametrize(
    ("table", "column", "value", "check"),
    [
        ("cash_flow_statement", "net_income", 60.0, "net_income_mismatch"),
        ("cash_flow_statement", "end_period_cash_flow", 80.0, "cash_mismatch"),
        ("income_statement", "gross_profit", 300.0, "gross_profit_mismatch"),
        ("balance_sheet", "common_stock_shares_outstanding", -5.0, "negative_shares"),
    ],
)
def test_each_check_on_a_planted_bad_row(lake, table, column, value, check):
    _clean(lake)
    upsert = getattr(lake, f"upsert_{table}")
    upsert(
        pd.DataFrame(
            [
                {
                    "ticker": "GOOD.US",
                    "period_end": date(2024, 12, 31),
                    "frequency": "A",
                    column: value,
                }
            ]
        )
    )
    audit_statements(lake)
    assert ("GOOD.US", check) in _flags(lake)


def test_quarters_that_do_not_sum_to_the_annual_figure(lake):
    _clean(lake)
    lake.upsert_income_statement(
        pd.DataFrame(
            [
                {
                    "ticker": "GOOD.US",
                    "period_end": date(2024, 12, 31),
                    "frequency": "A",
                    "revenue": 500.0,
                }
            ]
        )
    )
    audit_statements(lake)
    # the annual revenue also breaks the gross-profit identity
    assert ("GOOD.US", "quarterly_sum_mismatch") in _flags(lake)


def test_filing_date_before_period_end(lake):
    _clean(lake)
    lake.upsert_income_statement(
        pd.DataFrame(
            [
                {
                    "ticker": "GOOD.US",
                    "period_end": date(2024, 12, 31),
                    "frequency": "A",
                    "filing_date": date(2024, 11, 1),
                }
            ]
        )
    )
    audit_statements(lake)
    assert ("GOOD.US", "filing_before_period_end") in _flags(lake)


def test_audit_is_idempotent_and_clears_fixed_rows(lake):
    _clean(lake)
    lake.upsert_balance_sheet(
        pd.DataFrame(
            [
                {
                    "ticker": "GOOD.US",
                    "period_end": date(2024, 12, 31),
                    "frequency": "A",
                    "total_liabilities": 900.0,
                }
            ]
        )
    )
    audit_statements(lake)
    audit_statements(lake)
    assert lake.count_rows("statement_flags") == 1
    lake.upsert_balance_sheet(
        pd.DataFrame(
            [
                {
                    "ticker": "GOOD.US",
                    "period_end": date(2024, 12, 31),
                    "frequency": "A",
                    "total_liabilities": 600.0,
                }
            ]
        )
    )
    audit_statements(lake)
    assert lake.count_rows("statement_flags") == 0


def test_audit_scoped_to_tickers_leaves_others_alone(lake):
    _clean(lake, "A.US")
    _clean(lake, "B.US")
    for t in ("A.US", "B.US"):
        lake.upsert_balance_sheet(
            pd.DataFrame(
                [
                    {
                        "ticker": t,
                        "period_end": date(2024, 12, 31),
                        "frequency": "A",
                        "total_liabilities": 900.0,
                    }
                ]
            )
        )
    audit_statements(lake, tickers=["A.US"])
    assert _flags(lake) == {("A.US", "balance_identity")}
    report = audit_statements(lake, tickers=["B.US"])
    assert report.counts == {"balance_identity": 1}
    assert _flags(lake) == {("A.US", "balance_identity"), ("B.US", "balance_identity")}


def test_missing_line_items_are_not_flagged(lake):
    lake.upsert_balance_sheet(
        pd.DataFrame(
            [
                {
                    "ticker": "BANK.US",
                    "period_end": date(2024, 12, 31),
                    "frequency": "A",
                    "total_assets": 1000.0,
                }
            ]
        )
    )
    audit_statements(lake)
    assert _flags(lake) == set()


def test_check_ids_are_unique():
    ids = [c.check_id for c in CHECKS]
    assert len(ids) == len(set(ids))


def test_statements_as_of_can_skip_flagged_rows(lake):
    _clean(lake)
    lake.upsert_balance_sheet(
        pd.DataFrame(
            [
                {
                    "ticker": "GOOD.US",
                    "period_end": date(2024, 12, 31),
                    "frequency": "A",
                    "total_liabilities": 900.0,
                }
            ]
        )
    )
    audit_statements(lake)
    kept = lake.get_statements_as_of("balance_sheet", "GOOD.US", date(2025, 6, 1))
    assert len(kept) == 1
    skipped = lake.get_statements_as_of(
        "balance_sheet", "GOOD.US", date(2025, 6, 1), exclude_flagged=True
    )
    assert skipped.empty
