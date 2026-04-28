"""End-to-end backtest with BuyAndHold on a canned synthetic price series.

Verifies that the backtester drives the Strategy + Broker seams correctly,
and that the resulting BacktestReport reflects the portfolio's PnL.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.types import Portfolio
from stonks.store.lake import DuckDBLake
from stonks.strategies.examples.buy_and_hold import BuyAndHold


@pytest.fixture
def lake_with_trend(tmp_path):
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()

    # Linear uptrend: 100 → 200 across 21 business days
    dates = pd.bdate_range(start="2026-01-02", periods=21)
    closes = [100.0 + 5.0 * i for i in range(len(dates))]
    df = pd.DataFrame(
        {
            "ticker": "AAPL.US",
            "date": [d.date() for d in dates],
            "open": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "adj_close": closes,
            "volume": [1_000_000] * len(dates),
        }
    )
    lake.upsert_prices(df)
    yield lake, dates
    lake.close()


def test_buy_and_hold_captures_the_uptrend(lake_with_trend):
    lake, dates = lake_with_trend
    portfolio = Portfolio(cash=10_000.0, positions={})
    broker = SimulatedBroker(portfolio=portfolio, slippage_bps=0.0, fee_per_trade=0.0)

    strategy = BuyAndHold({"ticker": "AAPL.US", "allocation": 1.0})

    config = BacktestConfig(
        start=dates[0].date(),
        end=dates[-1].date(),
        universe=["AAPL.US"],
        threshold=0.0,
    )
    bt = Backtester(strategies=[strategy], broker=broker, lake=lake, config=config)
    report = bt.run()

    # Bought ~100 shares near day 0 price (~100), rode to 200 → ~2x
    final_value = report.equity_curve[-1]
    assert final_value == pytest.approx(20_000.0, rel=0.02)
    assert report.final_return > 0.9  # roughly doubled


def test_report_has_sharpe_and_drawdown_metrics(lake_with_trend):
    lake, dates = lake_with_trend
    broker = SimulatedBroker(
        portfolio=Portfolio(cash=10_000.0, positions={}),
        slippage_bps=0.0,
        fee_per_trade=0.0,
    )
    strategy = BuyAndHold({"ticker": "AAPL.US", "allocation": 1.0})
    config = BacktestConfig(
        start=dates[0].date(),
        end=dates[-1].date(),
        universe=["AAPL.US"],
        threshold=0.0,
    )
    report = Backtester([strategy], broker, lake, config).run()
    assert hasattr(report, "sharpe")
    assert hasattr(report, "max_drawdown")
    assert hasattr(report, "cagr")
    assert len(report.equity_curve) == len(report.equity_dates)


def test_backtester_skips_days_with_no_prices(lake_with_trend):
    lake, dates = lake_with_trend
    broker = SimulatedBroker(
        portfolio=Portfolio(cash=10_000.0, positions={}),
        slippage_bps=0.0,
        fee_per_trade=0.0,
    )
    strategy = BuyAndHold({"ticker": "AAPL.US", "allocation": 1.0})
    config = BacktestConfig(
        start=date(2025, 1, 1),  # before any price data
        end=dates[-1].date(),
        universe=["AAPL.US"],
        threshold=0.0,
    )
    report = Backtester([strategy], broker, lake, config).run()
    # Should still complete and end up with positive return
    assert report.final_return > 0
