"""QualityValue end to end through the Backtester: it trades only once the
statements it needs are filed, and ends up holding the best-scored name."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.types import Portfolio
from stonks.store.lake import DuckDBLake
from stonks.strategies.examples.quality_value import QualityValue

QUARTERS = [date(2023, 3, 31), date(2023, 6, 30), date(2023, 9, 30), date(2023, 12, 31)]


def _write(lake: DuckDBLake, ticker: str, net_income: float, price: float) -> None:
    rows = [
        {
            "ticker": ticker,
            "period_end": q,
            "frequency": "Q",
            "filing_date": q + timedelta(days=35),
        }
        for q in QUARTERS
    ]
    lake.upsert_income_statement(
        pd.DataFrame(
            [{**r, "revenue": 100.0, "gross_profit": 40.0, "net_income": net_income} for r in rows]
        )
    )
    lake.upsert_cash_flow_statement(
        pd.DataFrame([{**r, "free_cash_flow": net_income} for r in rows])
    )
    lake.upsert_balance_sheet(
        pd.DataFrame(
            [
                {
                    **r,
                    "total_stockholder_equity": 200.0,
                    "total_assets": 400.0,
                    "total_liabilities": 200.0,
                    "common_stock_shares_outstanding": 100.0,
                }
                for r in rows
            ]
        )
    )
    days = pd.bdate_range("2023-01-02", "2024-06-28")
    lake.upsert_prices(
        pd.DataFrame(
            {
                "ticker": ticker,
                "date": [d.date() for d in days],
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "adj_close": price,
                "volume": 1_000,
            }
        )
    )


def test_backtest_waits_for_filings_then_holds_the_top_name(tmp_path):
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    try:
        _write(lake, "CHEAP.US", net_income=10.0, price=10.0)
        _write(lake, "PRICEY.US", net_income=1.0, price=50.0)
        broker = SimulatedBroker(Portfolio(cash=10_000.0))
        config = BacktestConfig(
            start=date(2023, 1, 2),
            end=date(2024, 6, 28),
            universe=["CHEAP.US", "PRICEY.US"],
            rebalance_every_bars=5,
        )
        Backtester([QualityValue({"top_k": 1})], broker, lake, config).run()

        fills = broker.reconcile()
        assert fills, "expected the strategy to trade once statements were filed"
        # The fourth quarter (needed for TTM) is filed 2024-02-04.
        assert min(f.filled_at.date() for f in fills) > date(2024, 2, 4)
        assert set(broker.fetch_portfolio().positions) == {"CHEAP.US"}
    finally:
        lake.close()
