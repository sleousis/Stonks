"""The vectorised fast path of more strategies (22.5): ``target_positions``
against the event engine. Single-ticker long/flat rules are exact on a
zero-cost fixture whose open is the previous close; the forecast
strategies size the ``vol_target`` way without the no-trade buffer, so
they are close, not exact."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.types import Portfolio
from stonks.lab.vectorized import load_closes, supports_vectorized, vectorized_backtest
from stonks.store.lake import DuckDBLake
from stonks.strategies.examples.donchian_breakout import DonchianBreakout
from stonks.strategies.examples.ewmac_trend import EWMACTrend
from stonks.strategies.examples.ma_crossover import MACrossoverStrategy
from stonks.strategies.examples.tsmom import TimeSeriesMomentum

DAYS = pd.bdate_range("2021-01-04", periods=620)
TICKERS = ["A.US", "B.US"]


def _closes(seed: int = 11) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    data = {}
    for i, t in enumerate(TICKERS):
        # regimes of drift so the trend rules switch on and off
        drift = np.repeat(rng.choice([-0.002, 0.0015, 0.0], size=len(DAYS) // 40 + 1), 40)
        data[t] = 40.0 * np.exp(np.cumsum(drift[: len(DAYS)] + rng.normal(0, 0.012, len(DAYS))))
        data[t] *= 1.0 + 0.1 * i
    return pd.DataFrame(data, index=DAYS)


def _lake(path, asset_class: str) -> DuckDBLake:
    closes = _closes()
    lake = DuckDBLake(path)
    lake.migrate()
    rows = []
    for t in TICKERS:
        c = closes[t].to_numpy()
        o = np.concatenate([[c[0]], c[:-1]])  # open = previous close
        rows.append(
            pd.DataFrame(
                {
                    "ticker": t,
                    "date": [d.date() for d in DAYS],
                    "open": o,
                    "high": np.maximum(o, c),
                    "low": np.minimum(o, c),
                    "close": c,
                    "adj_close": c,
                    "volume": 1e12,
                }
            )
        )
    lake.upsert_prices(pd.concat(rows, ignore_index=True))
    for t in TICKERS:
        lake.con.execute(
            "INSERT INTO instruments (id, asset_class) VALUES (?, ?)", [t, asset_class]
        )
    return lake


@pytest.fixture
def equity_lake(tmp_path):
    lake = _lake(tmp_path / "eq.duckdb", "equity")
    yield lake
    lake.close()


@pytest.fixture
def crypto_lake(tmp_path):
    lake = _lake(tmp_path / "cc.duckdb", "crypto")
    yield lake
    lake.close()


def _both(lake, strategy, tickers, start_bar=300):
    start, end = DAYS[start_bar].date(), DAYS[-1].date()
    config = BacktestConfig(start=start, end=end, universe=tickers)
    broker = SimulatedBroker(Portfolio(cash=1_000_000.0))
    report = Backtester([strategy], broker, lake, config).run()
    engine = np.asarray(report.equity_curve, dtype=float)
    engine_returns = engine[1:] / engine[:-1] - 1.0
    closes = load_closes(lake, tickers, end)
    weights = type(strategy).target_positions(closes, strategy.params)
    fast = vectorized_backtest(closes, weights, start=DAYS[start_bar + 1]).returns.to_numpy()
    assert len(fast) == len(engine_returns)
    return fast, engine_returns


@pytest.mark.parametrize(
    "cls", [MACrossoverStrategy, DonchianBreakout, EWMACTrend, TimeSeriesMomentum]
)
def test_the_strategies_have_the_fast_path(cls):
    assert supports_vectorized(cls)


@pytest.mark.parametrize(
    ("cls", "params"),
    [
        (MACrossoverStrategy, {"fast": 8, "slow": 40, "interval": "1d", "ticker": "A.US"}),
        (DonchianBreakout, {"lookback": 30, "ticker": "A.US"}),
    ],
)
def test_single_ticker_rules_match_the_event_engine(crypto_lake, cls, params):
    fast, engine = _both(crypto_lake, cls(params), ["A.US"])
    assert np.count_nonzero(fast), "the rule should trade in the window"
    np.testing.assert_allclose(fast, engine, atol=1e-9)


@pytest.mark.parametrize(
    ("cls", "params"),
    [
        (EWMACTrend, {"speeds": "8,16,32,64"}),
        (EWMACTrend, {"speeds": "16"}),
        (TimeSeriesMomentum, {"lookbacks": "125,250"}),
        (TimeSeriesMomentum, {"lookbacks": "125", "rebalance": "daily"}),
    ],
)
def test_forecast_strategies_track_the_event_engine_closely(equity_lake, cls, params):
    fast, engine = _both(equity_lake, cls(params), ["A.US"])
    assert np.std(engine) > 0
    corr = float(np.corrcoef(fast, engine)[0, 1])
    total_fast, total_engine = np.prod(1 + fast) - 1, np.prod(1 + engine) - 1
    assert corr > 0.95, corr
    assert abs(total_fast - total_engine) < 0.04, (total_fast, total_engine)


@pytest.mark.parametrize(
    ("cls", "params"),
    [
        (MACrossoverStrategy, {"fast": 8, "slow": 40, "interval": "1d", "ticker": "A.US"}),
        (DonchianBreakout, {"lookback": 30, "ticker": "A.US"}),
        (EWMACTrend, {"speeds": "8,16,32,64"}),
        (TimeSeriesMomentum, {"lookbacks": "125,250"}),
    ],
)
def test_weights_never_read_later_rows(cls, params):
    closes = _closes()
    full = cls.target_positions(closes, params)
    cut = 450
    shocked = closes.copy()
    shocked.iloc[cut:] *= np.linspace(0.3, 3.0, len(closes) - cut)[:, None]
    again = cls.target_positions(shocked, params)
    pd.testing.assert_frame_equal(full.iloc[:cut], again.iloc[:cut])


def test_forecast_weights_share_the_budget_and_cap_gross():
    closes = _closes()
    weights = EWMACTrend.target_positions(closes, {"speeds": "8,16,32,64"})
    assert (weights >= 0).all().all()  # long only by default
    assert (weights.abs().sum(axis=1) <= 1.0 + 1e-12).all()
    assert (weights.iloc[-100:].abs().sum(axis=1) > 0).any()


def test_the_fast_path_refuses_what_it_cannot_replay():
    closes = _closes()
    with pytest.raises(ValueError, match="daily"):
        MACrossoverStrategy.target_positions(closes, {"ticker": "A.US"})  # default 1h
    with pytest.raises(ValueError, match="no closes"):
        DonchianBreakout.target_positions(closes, {"ticker": "ZZZ.US"})
    with pytest.raises(ValueError, match="fixed"):
        EWMACTrend.target_positions(closes, {"scalar_mode": "estimate"})
    with pytest.raises(ValueError, match="fixed"):
        TimeSeriesMomentum.target_positions(closes, {"fdm_mode": "estimate"})
