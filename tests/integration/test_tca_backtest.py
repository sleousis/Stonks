"""BL-32: a backtest prices its simulated fills with the same shortfall
math as live orders. Orders decided at bar t's close fill at bar t+1's open,
so the delay is the overnight move and the impact what the cost model adds."""

from __future__ import annotations

import pandas as pd
import pytest

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.types import Portfolio
from stonks.production.tca import backtest_shortfalls, summarize
from stonks.store.lake import DuckDBLake
from stonks.strategies.examples.buy_and_hold import BuyAndHold


@pytest.fixture
def lake(tmp_path):
    lake = DuckDBLake(tmp_path / "lake.duckdb")
    lake.migrate()
    dates = pd.bdate_range(start="2026-01-02", periods=10)
    closes = [100.0 + 5.0 * i for i in range(len(dates))]
    lake.upsert_prices(
        pd.DataFrame(
            {
                "ticker": "AAPL.US",
                "date": [d.date() for d in dates],
                # each day opens 1 above the previous close
                "open": [c - 4.0 for c in closes],
                "high": [c + 1 for c in closes],
                "low": [c - 5 for c in closes],
                "close": closes,
                "adj_close": closes,
                "volume": [1_000_000] * len(dates),
            }
        )
    )
    yield lake, dates
    lake.close()


def test_backtest_shortfall_splits_delay_and_impact(lake):
    lake, dates = lake
    broker = SimulatedBroker(portfolio=Portfolio(cash=10_000.0), slippage_bps=10.0)
    config = BacktestConfig(
        start=dates[0].date(), end=dates[-1].date(), universe=["AAPL.US"], threshold=0.0
    )
    bt = Backtester(
        strategies=[BuyAndHold({"ticker": "AAPL.US", "allocation": 0.5})],
        broker=broker,
        lake=lake,
        config=config,
    )
    bt.run()
    rows = backtest_shortfalls(broker.fills, bt.decision_prices, broker.reference_price)
    first = rows[0].shortfall
    # decided at the first close (100), arrived at the next open (101)
    assert first.decision_price == pytest.approx(100.0)
    assert first.arrival_price == pytest.approx(101.0)
    assert first.delay_bps == pytest.approx(100.0)
    # 10 bps slippage on the open, in bps of the decision price
    assert first.impact_bps == pytest.approx(10.0 * 101.0 / 100.0)
    [total] = summarize(rows)
    assert total.is_bps == pytest.approx(first.is_bps)
