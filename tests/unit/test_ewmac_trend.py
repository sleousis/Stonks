"""EWMACTrend (Carver): 4-speed EWMAC, scaled, capped, FDM-combined, long
only, sized by vol_target."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.protocols import Strategy
from stonks.core.types import Portfolio
from stonks.features.forecast import EWMAC_FORECAST_SCALARS, ewmac_raw
from stonks.lab.catalog import strategy_catalog
from stonks.strategies.base import strategy_metadata
from stonks.strategies.examples.ewmac_trend import EWMACTrend
from tests.unit.nt888_helpers import write_bars
from tests.unit.trend_helpers import DATES, LAST, build_lake, trend

SERIES = {
    "UP.US": trend(0.003, seed=1),
    "DOWN.US": trend(-0.003, seed=2),
    "YOUNG.US": trend(0.003, seed=3)[-200:],  # listed 200 sessions ago
    "STALE.US": trend(0.003, seed=4)[:-30],  # stopped trading 30 sessions ago
}


@pytest.fixture(scope="module")
def lake(tmp_path_factory):
    series = dict(SERIES)
    stale = series.pop("STALE.US")
    db = build_lake(tmp_path_factory.mktemp("ewmac") / "lake.duckdb", series)
    write_bars(db, "STALE.US", DATES[: len(stale)], stale)
    yield db
    db.close()


def _closes(ticker: str, n: int | None = None) -> pd.Series:
    closes = pd.Series(SERIES[ticker])
    return closes if n is None else closes.iloc[-n:].reset_index(drop=True)


# ---- surface -------------------------------------------------------------------------


def test_catalogued_with_metadata():
    assert strategy_catalog()["ewmac_trend"] is EWMACTrend
    meta = strategy_metadata(EWMACTrend)
    assert meta.alpha_family == "trend"
    assert meta.premise == "trend"
    assert meta.required_history_bars == 256
    assert meta.label_horizon_bars == 21
    assert meta.applicable_asset_classes == ("equity", "crypto", "commodity")
    assert "Fails" in meta.hypothesis
    assert isinstance(EWMACTrend({}), Strategy)


def test_default_params():
    s = EWMACTrend({})
    assert s.speeds() == [8, 16, 32, 64]
    assert s.params["vol_span"] == 36
    assert s.params["fdm"] == 1.1
    assert s.params["scalar_mode"] == "fixed"


# ---- forecasts -----------------------------------------------------------------------


def test_rule_forecasts_are_scaled_capped_raw_ewmac():
    s = EWMACTrend({})
    closes = _closes("UP.US")
    rules = s.rule_forecasts(closes)
    assert list(rules.columns) == ["ewmac8", "ewmac16", "ewmac32", "ewmac64"]
    for fast in (8, 16, 32, 64):
        expected = (ewmac_raw(closes, fast) * EWMAC_FORECAST_SCALARS[fast]).clip(-20, 20)
        pd.testing.assert_series_equal(rules[f"ewmac{fast}"], expected, check_names=False)


def test_forecast_is_the_fdm_scaled_mean_of_the_rules_over_the_last_window(lake):
    s = EWMACTrend({})
    window = _closes("UP.US", 1024)
    last = s.rule_forecasts(window).iloc[-1]
    expected = float(np.clip(1.1 * last.mean(), -20, 20))
    assert s.forecast("UP.US", LAST, lake) == pytest.approx(expected)


def test_up_trend_is_long_and_down_trend_is_flat(lake):
    s = EWMACTrend({})
    up = s.estimate_return("UP.US", LAST, lake)
    assert up is not None and 0 < up <= 20
    assert s.forecast("DOWN.US", LAST, lake) < 0
    assert s.estimate_return("DOWN.US", LAST, lake) is None


def test_forecast_is_capped_at_twenty(tmp_path):
    # a noiseless exponential: every speed's raw signal is many sigmas
    rocket = 50.0 * np.exp(0.002 * np.arange(len(DATES)))
    db = build_lake(tmp_path / "l.duckdb", {"R.US": rocket})
    try:
        assert EWMACTrend({}).forecast("R.US", LAST, db) == 20.0
    finally:
        db.close()


def test_too_little_history_or_stale_bars_give_no_forecast(lake):
    s = EWMACTrend({})
    assert s.forecast("YOUNG.US", LAST, lake) is None
    assert s.forecast("STALE.US", LAST, lake) is None
    assert s.forecast("NOPE.US", LAST, lake) is None
    assert s.estimate_return("UP.US", LAST, None) is None
    # a faster speed set needs less history
    assert EWMACTrend({"speeds": "4,8,16,32"}).forecast("YOUNG.US", LAST, lake) is not None


def test_single_speed_has_no_fdm(lake):
    s = EWMACTrend({"speeds": "16"})
    last = s.rule_forecasts(_closes("UP.US", 256)).iloc[-1, 0]
    assert s.forecast("UP.US", LAST, lake) == pytest.approx(last)


def test_estimated_fdm_is_used_in_estimate_mode(lake):
    fixed = EWMACTrend({}).forecast("UP.US", LAST, lake)
    est = EWMACTrend({"fdm_mode": "estimate"}).forecast("UP.US", LAST, lake)
    # the four speeds are strongly but not perfectly correlated
    assert est != fixed
    assert 0 < est <= 20


@pytest.mark.parametrize(
    "params", [{}, {"scalar_mode": "estimate"}, {"fdm_mode": "estimate"}], ids=str
)
def test_forecast_never_reads_bars_after_as_of(tmp_path, params):
    closes = SERIES["UP.US"]
    changed = closes.copy()
    changed[600:] *= np.linspace(1.0, 0.2, len(changed) - 600)
    a = build_lake(tmp_path / "a.duckdb", {"UP.US": closes})
    b = build_lake(tmp_path / "b.duckdb", {"UP.US": changed})
    try:
        as_of = DATES[599].to_pydatetime()
        s = EWMACTrend(params)
        assert s.forecast("UP.US", as_of, a) == s.forecast("UP.US", as_of, b)
    finally:
        a.close()
        b.close()


def test_features_expose_forecast_and_sigma(lake):
    values = EWMACTrend({}).extract_features("UP.US", LAST, lake).values
    assert set(values) == {"forecast", "sigma_annual"}
    assert 0.05 < values["sigma_annual"] < 0.5  # 1% daily noise


def test_crypto_sigma_annualises_over_365_days(tmp_path):
    db = build_lake(
        tmp_path / "c.duckdb",
        {"UP.US": SERIES["UP.US"], "BTC.CC": SERIES["UP.US"]},
        asset_classes={"BTC.CC": "crypto"},
    )
    try:
        s = EWMACTrend({})
        eq = s.extract_features("UP.US", LAST, db).values["sigma_annual"]
        cc = s.extract_features("BTC.CC", LAST, db).values["sigma_annual"]
        assert cc / eq == pytest.approx(np.sqrt(365 / 252))
    finally:
        db.close()


# ---- sizing --------------------------------------------------------------------------


def test_decide_sizes_with_vol_target_and_the_buffer(lake):
    s = EWMACTrend({})
    f = s.estimate_return("UP.US", LAST, lake)
    sigma = s.extract_features("UP.US", LAST, lake).values["sigma_annual"]
    price = float(SERIES["UP.US"][-1])
    orders = s.decide([(f, "UP.US")], Portfolio(cash=100_000.0), {"UP.US": price}, LAST)
    weight = min(1.0, 0.20 * f / 10.0 / sigma)  # one instrument: IDM 1, iw 1
    assert [(o.side, o.ticker) for o in orders] == [("buy", "UP.US")]
    # from flat the trade goes to the lower edge of the 10% band
    assert orders[0].quantity == pytest.approx(0.9 * weight * 100_000.0 / price)


def test_flat_names_count_towards_the_instrument_weights(lake):
    s = EWMACTrend({"tau": 0.05})  # low enough that no weight hits the gross cap
    price = float(SERIES["UP.US"][-1])
    prices = {"UP.US": price, "DOWN.US": float(SERIES["DOWN.US"][-1])}
    f = s.estimate_return("UP.US", LAST, lake)
    alone = s.decide([(f, "UP.US")], Portfolio(cash=100_000.0), prices, LAST)[0].quantity
    s.estimate_return("DOWN.US", LAST, lake)
    shared = s.decide([(f, "UP.US")], Portfolio(cash=100_000.0), prices, LAST)
    assert [o.ticker for o in shared] == ["UP.US"]
    assert shared[0].quantity < alone


def test_decide_never_spends_more_than_cash(lake):
    s = EWMACTrend({"tau": 0.40})
    f = s.estimate_return("UP.US", LAST, lake)
    price = float(SERIES["UP.US"][-1])
    portfolio = Portfolio(cash=1_000.0)
    orders = s.decide([(f, "UP.US")], portfolio, {"UP.US": price}, LAST)
    assert sum(o.quantity * price for o in orders if o.side == "buy") <= 1_000.0 + 1e-9


def test_decide_sells_a_name_whose_forecast_turned_negative(lake):
    s = EWMACTrend({})
    s.estimate_return("DOWN.US", LAST, lake)
    portfolio = Portfolio(cash=0.0, positions={"DOWN.US": 7.0})
    orders = s.decide([], portfolio, {"DOWN.US": 10.0}, LAST)
    assert [(o.side, o.ticker, o.quantity) for o in orders] == [("sell", "DOWN.US", 7.0)]


def test_fresh_instance_only_exits(lake):
    s = EWMACTrend({})  # never evaluated the day
    portfolio = Portfolio(cash=500.0, positions={"UP.US": 3.0, "DOWN.US": 2.0})
    prices = {"UP.US": 100.0, "DOWN.US": 10.0}
    orders = s.decide([(12.0, "UP.US")], portfolio, prices, LAST)
    assert [(o.side, o.ticker, o.quantity) for o in orders] == [("sell", "DOWN.US", 2.0)]


def test_decide_on_another_day_than_evaluated_only_exits(lake):
    s = EWMACTrend({})
    s.estimate_return("UP.US", LAST - timedelta(days=1), lake)
    orders = s.decide([(12.0, "UP.US")], Portfolio(cash=1_000.0), {"UP.US": 100.0}, LAST)
    assert orders == []


# ---- backtest ------------------------------------------------------------------------


def test_small_backtest_holds_the_up_trend_and_never_goes_negative_cash(lake):
    broker = SimulatedBroker(Portfolio(cash=100_000.0), slippage_bps=0.0, fee_per_trade=0.0)
    config = BacktestConfig(
        start=DATES[450].date(), end=date(2023, 9, 29), universe=["UP.US", "DOWN.US"]
    )
    report = Backtester([EWMACTrend({})], broker, lake, config).run()
    book = broker.fetch_portfolio()
    assert book.positions.get("UP.US", 0.0) > 0
    assert book.positions.get("DOWN.US", 0.0) == 0.0
    assert book.cash >= -1e-6
    assert report.equity_curve[-1] > 100_000.0
