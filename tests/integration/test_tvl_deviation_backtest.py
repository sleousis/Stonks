"""Daily Backtester run of TVLDeviationStrategy on a tmp lake holding bars
and a DeFi TVL series (hermetic)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.interval import Interval
from stonks.core.types import Portfolio
from stonks.store.lake import DuckDBLake
from stonks.strategies.examples.tvl_deviation import TVLDeviationStrategy

T = "ETH-USD.CC"
N = 200
START = date(2025, 1, 1)


def _seed(path) -> DuckDBLake:
    rng = np.random.default_rng(11)
    tvl = 5e10 * np.exp(np.cumsum(rng.normal(0, 0.02, N)))
    # Price follows yesterday's TVL plus mean-reverting noise, so it drifts
    # above and below the TVL-implied level.
    noise = np.zeros(N)
    for i in range(1, N):
        noise[i] = 0.6 * noise[i - 1] + rng.normal(0, 0.03)
    close = np.concatenate([[tvl[0]], tvl[:-1]]) / 2.5e7 * np.exp(noise)
    opens = np.concatenate([[close[0]], close[:-1]])
    days = [START + timedelta(days=i) for i in range(N)]
    bars = pd.DataFrame(
        {
            "ticker": T,
            "timestamp": [datetime.combine(d, datetime.min.time()) for d in days],
            "open": opens,
            "high": np.maximum(opens, close) * 1.01,
            "low": np.minimum(opens, close) * 0.99,
            "close": close,
            "adj_close": close,
            "volume": 1e6,
        }
    )
    lake = DuckDBLake(path)
    lake.migrate()
    lake.upsert_bars(bars, interval=Interval.DAY_1)
    lake.upsert_defi_tvl(
        pd.DataFrame(
            {"chain": "ethereum", "observation_date": days, "tvl_usd": tvl, "source": "test"}
        )
    )
    return lake


def test_backtest_runs_and_trades(tmp_path):
    lake = _seed(tmp_path / "lake.duckdb")
    try:
        broker = SimulatedBroker(portfolio=Portfolio(cash=10_000.0, positions={}))
        strategy = TVLDeviationStrategy({"ticker": T, "fit_length": 7, "atr_lookback": 20})
        config = BacktestConfig(
            start=datetime.combine(START, datetime.min.time()),
            end=datetime.combine(START + timedelta(days=N - 1), datetime.min.time()),
            universe=[T],
            interval=Interval.DAY_1,
        )
        report = Backtester([strategy], broker, lake, config).run()
    finally:
        lake.close()

    assert len(report.equity_curve) == N
    fills = broker.reconcile()
    buys = [f for f in fills if f.side == "buy"]
    sells = [f for f in fills if f.side == "sell"]
    assert buys, "strategy should enter at least once"
    assert sells, "strategy should exit at least once"
    assert all(f.ticker == T for f in fills)
    assert all(q >= 0 for q in broker.fetch_portfolio().positions.values())
    assert report.equity_curve[-1] > 0
