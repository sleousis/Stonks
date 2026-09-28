"""A small lake for screener tests: three names with price paths,
statements, a market cap and dividends (no network)."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from stonks.ingest.pipeline import _rows_to_df
from stonks.ingest.schemas import TickerProfile

START, END = date(2024, 1, 1), date(2024, 12, 31)


def seed_path(lake, ticker: str, first: float, last: float, *, volume: int = 100_000) -> None:
    days = pd.bdate_range(START, END)
    closes = np.linspace(first, last, len(days))
    lake.upsert_prices(
        pd.DataFrame(
            {
                "ticker": ticker,
                "date": [d.date() for d in days],
                "open": closes,
                "high": closes * 1.01,
                "low": closes * 0.99,
                "close": closes,
                "adj_close": closes,
                "volume": volume,
            }
        )
    )


def _quarter(ticker: str, period_end: str, filed: str, **values: float) -> dict:
    return {
        "ticker": ticker,
        "period_end": date.fromisoformat(period_end),
        "frequency": "Q",
        "filing_date": date.fromisoformat(filed),
        "currency": "USD",
        **values,
    }


def seed_market(lake):
    """AAA rises 10 to 20 and earns; BBB is flat at 50 and pays 2 a year;
    CCC falls 30 to 15 in another sector."""
    seed_path(lake, "AAA.US", 10.0, 20.0)
    seed_path(lake, "BBB.US", 50.0, 50.0)
    seed_path(lake, "CCC.US", 30.0, 15.0)
    lake.upsert_instrument_profile(
        _rows_to_df(
            [
                TickerProfile(id="AAA.US", exchange="NYSE", sector="Tech", name="Aaa Inc"),
                TickerProfile(id="BBB.US", exchange="NYSE", sector="Tech", name="Bbb Inc"),
                TickerProfile(id="CCC.US", exchange="NYSE", sector="Energy", name="Ccc Inc"),
            ]
        )
    )
    quarters = [
        ("2022-12-31", "2023-02-01", 8e6, 5e5),
        ("2023-03-31", "2023-05-01", 8e6, 5e5),
        ("2023-06-30", "2023-08-01", 8e6, 5e5),
        ("2023-09-30", "2023-11-01", 8e6, 5e5),
        ("2023-12-31", "2024-02-01", 8e6, 5e5),
        ("2024-03-31", "2024-05-01", 1e7, 1e6),
        ("2024-06-30", "2024-08-01", 1e7, 1e6),
        ("2024-09-30", "2024-11-01", 1e7, 1e6),
        ("2024-12-31", "2025-02-01", 1e7, 9e9),  # filed after the screen date
    ]
    rows = [_quarter("AAA.US", p, f, revenue=r, net_income=n) for p, f, r, n in quarters]
    rows.append(_quarter("CCC.US", "2024-09-30", "2024-11-01", revenue=1e7, net_income=-1e6))
    lake.upsert_income_statement(pd.DataFrame(rows))
    lake.upsert_balance_sheet(
        pd.DataFrame(
            [
                _quarter(
                    "AAA.US",
                    "2024-09-30",
                    "2024-11-01",
                    total_stockholder_equity=2e7,
                    short_long_term_debt_total=1e7,
                    common_stock_shares_outstanding=1e6,
                )
            ]
        )
    )
    lake.upsert_market_cap_history(
        pd.DataFrame([{"ticker": "AAA.US", "date": date(2024, 12, 30), "market_cap": 2e8}])
    )
    lake.upsert_dividends(
        pd.DataFrame(
            [
                {
                    "ticker": "BBB.US",
                    "ex_date": date(2024, m, 15),
                    "amount": 0.5,
                    "currency": "USD",
                    "pay_date": None,
                    "record_date": None,
                    "declaration_date": None,
                }
                for m in (3, 6, 9, 12)
            ]
        )
    )
    return lake
