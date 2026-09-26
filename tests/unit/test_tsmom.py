"""TimeSeriesMomentum: sign and vol-scaled lookback returns, FDM-combined,
monthly refresh, long only, sized by vol_target."""

from __future__ import annotations

import math
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.protocols import Strategy
from stonks.core.types import Portfolio
from stonks.features.forecast import TSMOM_SCALED_SCALAR, tsmom_raw
from stonks.lab.catalog import strategy_catalog
from stonks.strategies.base import strategy_metadata
from stonks.strategies.examples.tsmom import TimeSeriesMomentum
from tests.unit.trend_helpers import DATES, LAST, build_lake, trend

N = len(DATES)
AUG_END = datetime(2023, 8, 31)  # last session of August; LAST is 2023-09-08


def _steps(*legs: tuple[int, float]) -> np.ndarray:
    """A noiseless series: ``(bars, daily log return)`` legs, oldest first."""
    returns = np.concatenate([np.full(n, r) for n, r in legs])
    return 50.0 * np.exp(np.cumsum(returns))


def _alternating(drift: float, amplitude: float) -> np.ndarray:
    signs = np.where(np.arange(N) % 2 == 0, 1.0, -1.0)
    return 50.0 * np.exp(np.cumsum(drift + amplitude * signs))


SERIES = {
    "UP.US": trend(0.003, seed=1),
    "DOWN.US": trend(-0.003, seed=2),
    # up for a year and a half, then down for the last 125 sessions:
    # the 250-day return is still positive, the 125-day one negative
    "TURN.US": _steps((N - 125, 0.004), (125, -0.002)),
    "CALM.US": _alternating(0.0002, 0.005),
    "WILD.US": _alternating(0.0002, 0.02),
}


@pytest.fixture(scope="module")
def lake(tmp_path_factory):
    db = build_lake(tmp_path_factory.mktemp("tsmom") / "lake.duckdb", SERIES)
    yield db
    db.close()


def _daily(**params):
    return TimeSeriesMomentum({"rebalance": "daily", **params})


# ---- surface -------------------------------------------------------------------------


def test_catalogued_with_metadata():
    assert strategy_catalog()["tsmom"] is TimeSeriesMomentum
    meta = strategy_metadata(TimeSeriesMomentum)
    assert meta.alpha_family == "trend"
    assert meta.required_history_bars == 251
    assert meta.label_horizon_bars == 21
    assert meta.applicable_asset_classes == ("equity", "crypto", "commodity")
    assert meta.hypothesis
    assert isinstance(TimeSeriesMomentum({}), Strategy)
    s = TimeSeriesMomentum({})
    assert s.lookbacks() == [125, 250]
    assert (s.params["mode"], s.params["rebalance"]) == ("sign", "monthly")


# ---- sign mode ------------------------------------------------------------------------


def test_sign_of_both_lookbacks_up_is_long(lake):
    s = _daily()
    assert s.forecast("UP.US", LAST, lake) == pytest.approx(11.0)  # 1.1 * 10
    assert s.estimate_return("UP.US", LAST, lake) == pytest.approx(11.0)


def test_sign_of_both_lookbacks_down_is_flat(lake):
    s = _daily()
    assert s.forecast("DOWN.US", LAST, lake) == pytest.approx(-11.0)
    assert s.estimate_return("DOWN.US", LAST, lake) is None


def test_disagreeing_lookbacks_cancel(lake):
    s = _daily()
    assert s.forecast("TURN.US", LAST, lake) == 0.0
    assert s.estimate_return("TURN.US", LAST, lake) is None
    assert _daily(lookbacks="250").forecast("TURN.US", LAST, lake) == pytest.approx(10.0)
    assert _daily(lookbacks="125").forecast("TURN.US", LAST, lake) == pytest.approx(-10.0)


# ---- scaled mode ---------------------------------------------------------------------


def test_scaled_forecast_by_hand(lake):
    s = _daily(mode="scaled")
    closes = pd.Series(SERIES["UP.US"])
    rules = [
        float(np.clip(tsmom_raw(closes, lb, "scaled").iloc[-1] * TSMOM_SCALED_SCALAR, -20, 20))
        for lb in (125, 250)
    ]
    expected = float(np.clip(1.1 * np.mean(rules), -20, 20))
    assert s.forecast("UP.US", LAST, lake) == pytest.approx(expected)


def test_scaled_forecast_falls_with_volatility(lake):
    s = _daily(mode="scaled", lookbacks="250")
    calm = s.forecast("CALM.US", LAST, lake)
    wild = s.forecast("WILD.US", LAST, lake)
    # same 250-day return (the alternation cancels), four times the sigma
    assert 0 < wild < calm < 20
    assert calm / wild == pytest.approx(math.hypot(0.0002, 0.02) / math.hypot(0.0002, 0.005), 0.05)


def test_scaled_forecast_is_capped(lake):
    s = _daily(mode="scaled")
    assert s.forecast("TURN.US", DATES[N - 130].to_pydatetime(), lake) == 20.0


# ---- rebalance -----------------------------------------------------------------------


def test_monthly_holds_the_last_month_end_forecast(lake):
    monthly = TimeSeriesMomentum({"mode": "scaled"})
    daily = _daily(mode="scaled")
    held = monthly.forecast("UP.US", LAST, lake)
    assert held == pytest.approx(daily.forecast("UP.US", AUG_END, lake))
    assert held != pytest.approx(daily.forecast("UP.US", LAST, lake))
    # on the month-end session itself the forecast refreshes
    assert monthly.forecast("UP.US", AUG_END, lake) == pytest.approx(held)


# ---- causality -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [{}, {"mode": "scaled", "rebalance": "daily"}, {"mode": "scaled", "scalar_mode": "estimate"}],
    ids=str,
)
def test_forecast_never_reads_bars_after_as_of(tmp_path, params):
    closes = SERIES["UP.US"]
    changed = closes.copy()
    changed[600:] *= np.linspace(1.0, 0.1, N - 600)
    a = build_lake(tmp_path / "a.duckdb", {"UP.US": closes})
    b = build_lake(tmp_path / "b.duckdb", {"UP.US": changed})
    try:
        s = TimeSeriesMomentum(params)
        as_of = DATES[599].to_pydatetime()
        assert s.forecast("UP.US", as_of, a) == s.forecast("UP.US", as_of, b)
    finally:
        a.close()
        b.close()


# ---- backtest ------------------------------------------------------------------------


def test_small_backtest_is_long_the_up_trend_only(lake):
    broker = SimulatedBroker(Portfolio(cash=100_000.0), slippage_bps=0.0, fee_per_trade=0.0)
    config = BacktestConfig(
        start=DATES[450].date(), end=LAST.date(), universe=["UP.US", "DOWN.US", "TURN.US"]
    )
    report = Backtester([TimeSeriesMomentum({})], broker, lake, config).run()
    book = broker.fetch_portfolio()
    assert book.positions.get("UP.US", 0.0) > 0
    assert book.positions.get("DOWN.US", 0.0) == 0.0
    assert book.cash >= -1e-6
    assert report.equity_curve[-1] > 100_000.0
