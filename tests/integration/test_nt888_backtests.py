"""Short hourly Backtester runs for every neurotrader888 indicator strategy
on a tmp DuckDBLake (the intramarket strategy with a second, reference
ticker that is not in the traded universe)."""

from __future__ import annotations

import pytest

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.interval import Interval
from stonks.core.types import Portfolio
from stonks.strategies.examples.intramarket_difference import IntramarketDifferenceStrategy
from stonks.strategies.examples.ma_crossover import MACrossoverStrategy
from stonks.strategies.examples.market_profile_sr import MarketProfileSRStrategy
from stonks.strategies.examples.visibility_graph_path import VisibilityGraphPathStrategy
from stonks.strategies.examples.volatility_hawkes import VolatilityHawkesStrategy
from stonks.strategies.examples.vsa import VSAStrategy
from tests.nt888_bars import as_of, random_walk, seed_lake

T, REF = "ETH.CC", "BTC.CC"
N = 480

CASES = [
    (VolatilityHawkesStrategy, {"kappa": 0.5, "quantile_lookback": 24, "norm_lookback": 50}),
    (VisibilityGraphPathStrategy, {"lookback": 12}),
    (VSAStrategy, {"norm_lookback": 48, "threshold": 0.5, "hold_bars": 6}),
    (MarketProfileSRStrategy, {"lookback": 100}),
    (IntramarketDifferenceStrategy, {"lookback": 6, "atr_lookback": 24, "reference_ticker": REF}),
    (MACrossoverStrategy, {"fast": 3, "slow": 10}),
]


@pytest.fixture(scope="module")
def lake(tmp_path_factory):
    frame = random_walk(N, seed=21)
    ref = random_walk(N, seed=22, start_price=40.0)
    lk = seed_lake(tmp_path_factory.mktemp("bt") / "lake.duckdb", {T: frame, REF: ref})
    yield lk, frame
    lk.close()


@pytest.mark.parametrize(("cls", "params"), CASES, ids=[c[0].__name__ for c in CASES])
def test_backtest_runs_and_trades(cls, params, lake):
    lk, frame = lake
    broker = SimulatedBroker(portfolio=Portfolio(cash=10_000.0, positions={}))
    strategy = cls({**params, "ticker": T})
    config = BacktestConfig(
        start=as_of(frame, 0),
        end=as_of(frame, -1),
        universe=[T],
        interval=Interval.HOUR_1,
    )
    report = Backtester([strategy], broker, lk, config).run()

    assert len(report.equity_curve) == N
    fills = broker.reconcile()
    assert any(f.quantity > 0 for f in fills), "strategy should enter at least once"
    assert all(f.ticker == T for f in fills)
    # long-only: never short
    assert all(q >= 0 for q in broker.fetch_portfolio().positions.values())
    assert report.equity_curve[-1] > 0
