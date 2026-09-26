"""MacroRegimeFilter through the registry and the Backtester."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.types import Portfolio
from stonks.lab.dataset import LabDataset
from stonks.registry.store import StrategyRegistry
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState
from stonks.strategies.examples.rsi_pca import RSIPCAStrategy
from stonks.strategies.macro_regime import MacroRegimeFilter


@pytest.fixture
def lake(tmp_path):
    db = DuckDBLake(tmp_path / "lake.duckdb")
    db.migrate()
    days = pd.bdate_range("2023-01-02", "2024-06-28")
    closes = 100.0 * np.exp(np.cumsum(np.random.default_rng(5).normal(0, 0.01, len(days))))
    db.upsert_prices(
        pd.DataFrame(
            {
                "ticker": "AAPL.US",
                "date": [d.date() for d in days],
                "open": closes,
                "high": closes,
                "low": closes,
                "close": closes,
                "adj_close": closes,
                "volume": 1_000,
            }
        )
    )
    db.upsert_macro_indicators(
        pd.DataFrame(
            [
                {
                    "country_iso": "USA",
                    "indicator": "unemployment_total_percent",
                    "observation_date": d,
                    "period": "quarterly",
                    "country_name": "United States",
                    "value": v,
                }
                for d, v in [
                    (date(2022, 12, 31), 3.6),
                    (date(2023, 6, 30), 4.6),  # +1.0: risk off once public
                    (date(2023, 12, 31), 4.0),  # -0.6: risk on again
                ]
            ]
        )
    )
    yield db
    db.close()


def _wrapped_buy_and_hold() -> MacroRegimeFilter:
    return MacroRegimeFilter(
        {
            "inner_class_path": "stonks.strategies.examples.buy_and_hold:BuyAndHold",
            "inner_params": {"ticker": "AAPL.US"},
            "transform": "change",
            "threshold": 0.5,
            "publication_lag_days": 30,
        }
    )


def test_backtest_exits_during_risk_off_and_re_enters_after(lake):
    broker = SimulatedBroker(Portfolio(cash=10_000.0))
    config = BacktestConfig(start=date(2023, 1, 2), end=date(2024, 6, 28), universe=["AAPL.US"])
    Backtester([_wrapped_buy_and_hold()], broker, lake, config).run()

    fills = [(f.side, f.filled_at.date()) for f in broker.reconcile()]
    sides = [s for s, _ in fills]
    assert sides == ["buy", "sell", "buy"]
    # risk off from 2023-07-30 (obs 2023-06-30 + 30d): exit fills the next session
    assert date(2023, 7, 31) <= fills[1][1] <= date(2023, 8, 2)
    # risk on from 2024-01-30 (obs 2023-12-31 + 30d)
    assert date(2024, 1, 30) <= fills[2][1] <= date(2024, 2, 1)


def test_registry_round_trips_the_wrapper_with_fitted_inner_state(lake, tmp_path):
    inner_params = {"ticker": "AAPL.US", "long_quantile": 0.9}
    wrapper = MacroRegimeFilter(
        {
            "inner_class_path": "stonks.strategies.examples.rsi_pca:RSIPCAStrategy",
            "inner_params": inner_params,
            "publication_lag_days": 30,
        }
    )
    wrapper.fit(
        LabDataset(lake=lake, universe=["AAPL.US"], start=date(2023, 1, 2), end=date(2024, 6, 28))
    )
    assert isinstance(wrapper.inner, RSIPCAStrategy) and wrapper.inner.is_fitted

    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    try:
        registry = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
        sid = registry.register(wrapper, reports=[])
        loaded = registry.load(sid)
    finally:
        state.close()

    assert isinstance(loaded, MacroRegimeFilter)
    assert loaded.params == wrapper.params
    assert loaded.id == wrapper.id
    assert isinstance(loaded.inner, RSIPCAStrategy) and loaded.inner.is_fitted
    np.testing.assert_array_equal(loaded.inner._coefs, wrapper.inner._coefs)
    for d in pd.bdate_range("2023-03-01", "2024-06-28", freq="10B"):
        got = loaded.extract_features("AAPL.US", d.date(), lake).values
        # JSON round trip of the fitted arrays can move the last ulp of a matmul
        assert got == pytest.approx(wrapper.extract_features("AAPL.US", d.date(), lake).values)
        expected = wrapper.estimate_return("AAPL.US", d.date(), lake)
        got_r = loaded.estimate_return("AAPL.US", d.date(), lake)
        assert (got_r is None) == (expected is None)
        if expected is not None:
            assert got_r == pytest.approx(expected)
