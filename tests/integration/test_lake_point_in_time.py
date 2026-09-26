"""Point-in-time read helpers on the lake: statements visible as of a date
(by filing date, or period end + a conservative lag when the filing date is
unknown) and macro observations visible after a publication lag."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from stonks.store.lake import DuckDBLake


@pytest.fixture
def lake(tmp_path):
    db = DuckDBLake(tmp_path / "lake.duckdb")
    db.migrate()
    db.upsert_income_statement(
        pd.DataFrame(
            [
                # filed normally
                {
                    "ticker": "A.US",
                    "period_end": date(2024, 3, 31),
                    "frequency": "Q",
                    "filing_date": date(2024, 5, 2),
                    "revenue": 100.0,
                    "net_income": 10.0,
                },
                # filing date unknown -> period_end + lag
                {
                    "ticker": "A.US",
                    "period_end": date(2024, 6, 30),
                    "frequency": "Q",
                    "filing_date": None,
                    "revenue": 110.0,
                    "net_income": 11.0,
                },
                # nonsense filing date before period end -> clamp to period end
                {
                    "ticker": "A.US",
                    "period_end": date(2024, 9, 30),
                    "frequency": "Q",
                    "filing_date": date(2024, 9, 1),
                    "revenue": 120.0,
                    "net_income": 12.0,
                },
                {
                    "ticker": "A.US",
                    "period_end": date(2023, 12, 31),
                    "frequency": "A",
                    "filing_date": date(2024, 2, 20),
                    "revenue": 400.0,
                    "net_income": 40.0,
                },
                {
                    "ticker": "B.US",
                    "period_end": date(2024, 3, 31),
                    "frequency": "Q",
                    "filing_date": date(2024, 4, 20),
                    "revenue": 5.0,
                },
            ]
        )
    )
    yield db
    db.close()


def test_statement_history_adds_available_date(lake):
    df = lake.get_statement_history("income_statement", "A.US", missing_filing_lag_days=90)
    assert list(df["period_end"]) == sorted(df["period_end"])
    avail = dict(zip(df["period_end"], df["available_date"], strict=True))
    assert avail[date(2024, 3, 31)] == date(2024, 5, 2)
    assert avail[date(2024, 6, 30)] == date(2024, 9, 28)  # + 90 days
    assert avail[date(2024, 9, 30)] == date(2024, 9, 30)  # clamped
    assert avail[date(2023, 12, 31)] == date(2024, 2, 20)
    assert set(df["ticker"]) == {"A.US"}
    assert "revenue" in df.columns


def test_statements_as_of_hides_unfiled_rows(lake):
    df = lake.get_statements_as_of("income_statement", "A.US", date(2024, 5, 1))
    assert list(df["period_end"]) == [date(2023, 12, 31)]
    df = lake.get_statements_as_of("income_statement", "A.US", date(2024, 5, 2))
    # newest first
    assert list(df["period_end"]) == [date(2024, 3, 31), date(2023, 12, 31)]


def test_statements_as_of_applies_missing_filing_lag(lake):
    q2 = date(2024, 6, 30)
    before = lake.get_statements_as_of(
        "income_statement", "A.US", date(2024, 9, 27), missing_filing_lag_days=90
    )
    assert q2 not in set(before["period_end"])
    on = lake.get_statements_as_of(
        "income_statement", "A.US", date(2024, 9, 28), missing_filing_lag_days=90
    )
    assert q2 in set(on["period_end"])


def test_statements_as_of_filters_frequency(lake):
    df = lake.get_statements_as_of("income_statement", "A.US", date(2025, 1, 1), frequency="A")
    assert list(df["frequency"]) == ["A"]


def test_statements_as_of_accepts_datetimes(lake):
    from datetime import datetime

    df = lake.get_statements_as_of("income_statement", "A.US", datetime(2024, 5, 2, 16, 0))
    assert date(2024, 3, 31) in set(df["period_end"])


def test_statement_helpers_reject_unknown_tables(lake):
    with pytest.raises(ValueError, match="statement"):
        lake.get_statement_history("bars", "A.US")
    with pytest.raises(ValueError, match="statement"):
        lake.get_statements_as_of("instruments; DROP TABLE bars", "A.US", date(2025, 1, 1))


def test_statement_history_of_unknown_ticker_is_empty_with_columns(lake):
    df = lake.get_statement_history("balance_sheet", "NOPE.US")
    assert df.empty
    assert {"period_end", "available_date", "total_assets"} <= set(df.columns)


# ---- macro -------------------------------------------------------------------


@pytest.fixture
def macro_lake(lake):
    lake.upsert_macro_indicators(
        pd.DataFrame(
            [
                {
                    "country_iso": "USA",
                    "indicator": "unemployment_total_percent",
                    "observation_date": date(2022, 12, 31),
                    "period": "annual",
                    "country_name": "United States",
                    "value": 3.6,
                },
                {
                    "country_iso": "USA",
                    "indicator": "unemployment_total_percent",
                    "observation_date": date(2023, 12, 31),
                    "period": "annual",
                    "country_name": "United States",
                    "value": 3.9,
                },
                {
                    "country_iso": "DEU",
                    "indicator": "unemployment_total_percent",
                    "observation_date": date(2023, 12, 31),
                    "period": "annual",
                    "country_name": "Germany",
                    "value": 3.0,
                },
            ]
        )
    )
    return lake


def test_macro_series_returns_one_series_oldest_first(macro_lake):
    df = macro_lake.get_macro_series("USA", "unemployment_total_percent", stamped_at="period_end")
    assert list(df["observation_date"]) == [date(2022, 12, 31), date(2023, 12, 31)]
    assert list(df["value"]) == [3.6, 3.9]
    assert list(df["available_date"]) == list(df["observation_date"])


def test_macro_series_as_of_respects_publication_lag(macro_lake):
    df = macro_lake.get_macro_series(
        "USA",
        "unemployment_total_percent",
        as_of=date(2024, 3, 29),
        publication_lag_days=90,
        stamped_at="period_end",
    )
    assert list(df["observation_date"]) == [date(2022, 12, 31)]
    df = macro_lake.get_macro_series(
        "USA",
        "unemployment_total_percent",
        as_of=date(2024, 3, 30),
        publication_lag_days=90,
        stamped_at="period_end",
    )
    assert list(df["observation_date"]) == [date(2022, 12, 31), date(2023, 12, 31)]
    assert df["available_date"].iloc[-1] == date(2024, 3, 30)


def test_shares_outstanding_history_oldest_first(lake):
    lake.upsert_shares_outstanding(
        pd.DataFrame(
            [
                {"ticker": "A.US", "date": date(2024, 6, 30), "shares": 1_000.0},
                {"ticker": "A.US", "date": date(2024, 3, 31), "shares": 900.0},
                {"ticker": "B.US", "date": date(2024, 3, 31), "shares": 5.0},
            ]
        )
    )
    df = lake.get_shares_outstanding("A.US")
    assert list(df["date"]) == [date(2024, 3, 31), date(2024, 6, 30)]
    assert list(df["shares"]) == [900.0, 1_000.0]


def test_macro_series_rejects_negative_lag(macro_lake):
    with pytest.raises(ValueError, match="lag"):
        macro_lake.get_macro_series("USA", "x", publication_lag_days=-1)


@pytest.mark.parametrize(
    ("observation_date", "period", "period_end"),
    [
        (date(2023, 1, 1), "annual", date(2023, 12, 31)),
        (date(2023, 4, 1), "quarterly", date(2023, 6, 30)),
        (date(2024, 2, 1), "monthly", date(2024, 2, 29)),
        (date(2023, 1, 1), None, date(2023, 12, 31)),  # unknown cadence: assume a year
    ],
)
def test_period_start_stamps_are_available_only_after_the_period_ends(
    lake, observation_date, period, period_end
):
    """Vendors like EODHD stamp an annual observation at the start of the
    year it describes (2023-01-01 = full-year 2023). It can't be known
    before that year is over, whatever the publication lag."""
    lake.upsert_macro_indicators(
        pd.DataFrame(
            [
                {
                    "country_iso": "USA",
                    "indicator": "x",
                    "observation_date": observation_date,
                    "period": period,
                    "country_name": "United States",
                    "value": 1.0,
                }
            ]
        )
    )
    df = lake.get_macro_series("USA", "x", publication_lag_days=10)  # default: period_start
    assert df["available_date"].iloc[0] == period_end + timedelta(days=10)


def test_macro_series_rejects_unknown_stamp(macro_lake):
    with pytest.raises(ValueError, match="stamped_at"):
        macro_lake.get_macro_series("USA", "x", stamped_at="middle")
