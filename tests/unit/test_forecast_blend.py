"""ForecastBlend (roadmap 22.7): EWMAC and TSMOM rules combined with
forecast weights fitted net of costs on training bars only (P12, P19)."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.protocols import Strategy
from stonks.core.types import Portfolio
from stonks.features.forecast import combine_forecasts
from stonks.features.forecast_weights import weight_estimator_names
from stonks.lab.catalog import strategy_catalog
from stonks.strategies.base import strategy_metadata
from stonks.strategies.examples.forecast_blend import ForecastBlend
from tests.unit.trend_helpers import DATES, LAST, build_lake, trend

SERIES = {
    "UP.US": trend(0.002, seed=1),
    "COIN.CC": trend(0.002, seed=1),  # the same path, but crypto costs
    "FLAT.US": trend(0.0, seed=6),
}


@pytest.fixture(scope="module")
def lake(tmp_path_factory):
    db = build_lake(
        tmp_path_factory.mktemp("blend") / "lake.duckdb",
        SERIES,
        asset_classes={"UP.US": "equity", "COIN.CC": "crypto", "FLAT.US": "equity"},
    )
    yield db
    db.close()


# ---- surface -------------------------------------------------------------------------


def test_catalogued_with_metadata():
    assert strategy_catalog()["forecast_blend"] is ForecastBlend
    meta = strategy_metadata(ForecastBlend)
    assert meta.alpha_family == "trend"
    assert meta.label_horizon_bars == 21
    assert "Fails" in meta.hypothesis
    assert isinstance(ForecastBlend({}), Strategy)


def test_default_params():
    s = ForecastBlend({})
    assert s.rules() == [
        "ewmac2",
        "ewmac4",
        "ewmac8",
        "ewmac16",
        "ewmac32",
        "ewmac64",
        "tsmom125",
        "tsmom250",
    ]
    assert s.params["weight_method"] == "handcraft"
    assert s.params["max_cost_sr"] == 0.13
    assert s.params["refit"] == "year"
    assert s.params["fdm_mode"] == "estimate"


def test_weight_method_choices_come_from_the_registry():
    spec = next(p for p in ForecastBlend.parameter_spec() if p.name == "weight_method")
    assert set(spec.bounds) == set(weight_estimator_names())


# ---- fits ----------------------------------------------------------------------------


def test_forecast_uses_the_fitted_weights_and_fdm(lake):
    s = ForecastBlend({})
    f = s.forecast("UP.US", LAST, lake)
    fit = s.forecast_weight_fits()["UP.US"]
    assert fit.status == "ok"
    assert sum(fit.weights.values()) == pytest.approx(1.0)
    # the fit ends before this year's first bar
    assert fit.fit_end < pd.Timestamp(LAST.year, 1, 1)
    closes = pd.Series(SERIES["UP.US"])
    raw = s.raw_rule_forecasts(closes.iloc[-s.window_bars() :].reset_index(drop=True))
    kept = list(fit.weights)
    scaled = pd.DataFrame({n: (raw[n] * fit.scalars[n]).clip(-20, 20) for n in kept})
    expected = combine_forecasts(scaled, fit.weights, fdm=fit.fdm).iloc[-1]
    assert f == pytest.approx(float(expected))


def test_dearer_instrument_drops_the_fast_rules(lake):
    s = ForecastBlend({})
    s.forecast("UP.US", LAST, lake)
    s.forecast("COIN.CC", LAST, lake)
    fits = s.forecast_weight_fits()
    equity = {r.name: r for r in fits["UP.US"].rules}
    crypto = {r.name: r for r in fits["COIN.CC"].rules}
    assert crypto["ewmac2"].dropped and "speed limit" in crypto["ewmac2"].reason
    assert not crypto["ewmac64"].dropped
    assert crypto["ewmac2"].cost_sr > equity["ewmac2"].cost_sr
    assert sum(r.dropped for r in crypto.values()) >= sum(r.dropped for r in equity.values())


def test_every_rule_too_dear_means_no_forecast(lake):
    s = ForecastBlend({"cost_multiplier": 100.0})
    assert s.forecast("COIN.CC", LAST, lake) is None
    assert s.forecast_weight_fits()["COIN.CC"].status == "too_costly"


@pytest.mark.parametrize("method", ["equal", "handcraft", "bootstrap"])
def test_each_estimator_fits(lake, method):
    s = ForecastBlend({"weight_method": method})
    assert s.forecast("UP.US", LAST, lake) is not None
    fit = s.forecast_weight_fits()["UP.US"]
    assert fit.method == method
    assert min(fit.weights.values()) >= 0


def test_monthly_refit_uses_more_recent_training_bars(lake):
    yearly = ForecastBlend({})
    monthly = ForecastBlend({"refit": "month"})
    yearly.forecast("UP.US", LAST, lake)
    monthly.forecast("UP.US", LAST, lake)
    y = yearly.forecast_weight_fits()["UP.US"]
    m = monthly.forecast_weight_fits()["UP.US"]
    assert m.fit_end > y.fit_end
    assert m.fit_end < pd.Timestamp(LAST.year, LAST.month, 1)


def test_fit_ignores_bars_after_the_training_window(tmp_path):
    """P12: bars dated after the fit window (this year's, still before the
    decision) change the forecast but never the weights."""
    base = trend(0.002, seed=1)
    changed = base.copy()
    this_year = np.asarray(DATES.year == LAST.year)
    changed[this_year] = changed[this_year] * np.linspace(1.0, 0.5, int(this_year.sum()))
    a = build_lake(tmp_path / "a.duckdb", {"X.US": base})
    b = build_lake(tmp_path / "b.duckdb", {"X.US": changed})
    try:
        sa, sb = ForecastBlend({}), ForecastBlend({})
        fa, fb = sa.forecast("X.US", LAST, a), sb.forecast("X.US", LAST, b)
        assert sa.forecast_weight_fits()["X.US"] == sb.forecast_weight_fits()["X.US"]
        assert fa != fb
    finally:
        a.close()
        b.close()


def test_short_history_is_a_warm_up(tmp_path):
    db = build_lake(tmp_path / "young.duckdb", {"Y.US": trend(0.002, seed=3)[-300:]})
    try:
        s = ForecastBlend({})
        assert s.forecast("Y.US", LAST, db) is not None
        fit = s.forecast_weight_fits()["Y.US"]
        assert fit.status == "warmup"
        assert not any(r.dropped for r in fit.rules)
    finally:
        db.close()


# ---- backtest ------------------------------------------------------------------------


def test_small_backtest_trades_and_keeps_cash_non_negative(lake):
    broker = SimulatedBroker(Portfolio(cash=100_000.0), slippage_bps=0.0, fee_per_trade=0.0)
    config = BacktestConfig(
        start=DATES[560].date(), end=date(2023, 9, 29), universe=["UP.US", "FLAT.US"]
    )
    s = ForecastBlend({})
    Backtester([s], broker, lake, config).run()
    book = broker.fetch_portfolio()
    assert book.positions.get("UP.US", 0.0) > 0
    assert book.cash >= -1e-6
    assert set(s.forecast_weight_fits()) >= {"UP.US"}
