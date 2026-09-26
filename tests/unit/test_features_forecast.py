"""Carver-style forecasts: EMA, EWMAC, TSMOM raw signals, scaling, capping
and combining (BL-40)."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from stonks.features.forecast import (
    EWMAC_FORECAST_SCALARS,
    TSMOM_SCALED_SCALAR,
    cap_forecast,
    combine_forecasts,
    ema,
    ewmac_raw,
    price_change_vol,
    tsmom_raw,
)
from stonks.portfolio.signals import FORECAST_CAP, FORECAST_TARGET


def _random_walk(n=4000, seed=7, drift=0.0):
    rng = np.random.default_rng(seed)
    return pd.Series(100.0 + np.cumsum(rng.normal(drift, 1.0, n)))


# ---- EMA and vol ------------------------------------------------------------------


def test_ema_is_the_recursive_average_seeded_at_the_first_price():
    prices = pd.Series([10.0, 13.0, 7.0, 10.0])
    # span 3 -> alpha 0.5
    assert ema(prices, 3).tolist() == [10.0, 11.5, 9.25, 9.625]


def test_ema_rejects_a_span_below_one():
    with pytest.raises(ValueError):
        ema(pd.Series([1.0, 2.0]), 0)


def test_price_change_vol_is_zero_mean_ewma_of_price_differences():
    prices = pd.Series(np.arange(50, dtype=float) * 2.0)  # constant +2 a bar
    sigma = price_change_vol(prices, span=10)
    assert sigma.iloc[:10].isna().all()  # warm-up
    assert sigma.iloc[-1] == pytest.approx(2.0)


# ---- EWMAC -----------------------------------------------------------------------


def test_ewmac_raw_by_hand():
    prices = pd.Series([10.0, 12.0, 11.0, 13.0, 14.0])
    raw = ewmac_raw(prices, fast=1, slow=3, vol_span=1)
    # fast span 1 is the price itself; slow span 3 has alpha 0.5
    slow = [10.0, 11.0, 11.0, 12.0, 13.0]
    # span 1 vol is |price change|
    vol = [math.nan, 2.0, 1.0, 2.0, 1.0]
    expected = [(p - s) / v for p, s, v in zip(prices, slow, vol, strict=True)]
    assert math.isnan(raw.iloc[0])
    assert raw.iloc[1:].tolist() == pytest.approx(expected[1:])


def test_ewmac_defaults_the_slow_span_to_four_times_fast():
    prices = _random_walk(300)
    assert ewmac_raw(prices, 8).equals(ewmac_raw(prices, 8, 32))


def test_ewmac_is_positive_on_an_up_ramp_and_negative_on_a_down_ramp():
    up = pd.Series(100.0 + np.arange(400, dtype=float))
    down = pd.Series(500.0 - np.arange(400, dtype=float))
    assert ewmac_raw(up, 16).iloc[-1] > 0
    assert ewmac_raw(down, 16).iloc[-1] < 0
    assert ewmac_raw(up, 16).iloc[-1] == pytest.approx(-ewmac_raw(down, 16).iloc[-1])


def test_ewmac_on_a_flat_series_is_undefined_not_infinite():
    raw = ewmac_raw(pd.Series(np.full(100, 5.0)), 8)
    assert not np.isinf(raw).any()
    assert raw.isna().all()


def test_ewmac_is_causal():
    prices = _random_walk(600)
    changed = prices.copy()
    changed.iloc[400:] *= 3.0
    a, b = ewmac_raw(prices, 16), ewmac_raw(changed, 16)
    pd.testing.assert_series_equal(a.iloc[:400], b.iloc[:400])


def test_carver_scalars_are_the_published_table():
    assert EWMAC_FORECAST_SCALARS == {2: 10.6, 4: 7.5, 8: 5.3, 16: 3.75, 32: 2.65, 64: 1.87}


def test_carver_scalars_are_consistent_across_speeds_on_a_random_walk():
    # Carver fit the table on real (trending) prices, whose raw EWMAC runs
    # larger; on a driftless random walk every speed lands near 8.2.
    means = []
    for fast in (4, 8, 16, 32):
        raw = ewmac_raw(_random_walk(50_000, seed=fast), fast).iloc[1000:]
        means.append((raw * EWMAC_FORECAST_SCALARS[fast]).abs().mean())
    assert all(7.0 < m < 0.95 * FORECAST_TARGET for m in means)
    assert max(means) / min(means) < 1.1


# ---- scaling and capping -----------------------------------------------------------


def test_cap_forecast_clips_both_ways_at_twenty():
    f = cap_forecast(pd.Series([-50.0, -20.0, 5.0, 20.0, 31.0]))
    assert f.tolist() == [-20.0, -20.0, 5.0, 20.0, 20.0]
    assert cap_forecast(33.0) == FORECAST_CAP
    assert cap_forecast(-3.0, cap=2.0) == -2.0


def test_cap_forecast_keeps_nan():
    assert math.isnan(cap_forecast(math.nan))


# ---- combining ---------------------------------------------------------------------


def test_combine_forecasts_weights_multiplies_by_fdm_and_caps():
    rules = pd.DataFrame({"a": [10.0, 20.0, -4.0], "b": [4.0, 20.0, -8.0]})
    out = combine_forecasts(rules, fdm=1.5)
    assert out.tolist() == pytest.approx([10.5, 20.0, -9.0])


def test_combine_forecasts_with_explicit_weights():
    rules = pd.DataFrame({"a": [10.0], "b": [0.0]})
    assert combine_forecasts(rules, weights={"a": 3.0, "b": 1.0}).iloc[0] == pytest.approx(7.5)


def test_combine_forecasts_ignores_rules_still_warming_up():
    rules = pd.DataFrame({"a": [math.nan, 6.0], "b": [4.0, 2.0]})
    out = combine_forecasts(rules)
    assert out.tolist() == pytest.approx([4.0, 4.0])


def test_combine_forecasts_is_nan_when_no_rule_has_a_value():
    out = combine_forecasts(pd.DataFrame({"a": [math.nan], "b": [math.nan]}))
    assert math.isnan(out.iloc[0])


def test_combine_forecasts_rejects_a_bad_fdm():
    with pytest.raises(ValueError):
        combine_forecasts(pd.DataFrame({"a": [1.0]}), fdm=0.0)


# ---- TSMOM -------------------------------------------------------------------------


def test_tsmom_sign_mode_is_the_sign_of_the_lookback_return():
    closes = pd.Series([10.0, 11.0, 9.0, 12.0, 12.0])
    raw = tsmom_raw(closes, lookback=2, mode="sign")
    assert raw.iloc[:2].isna().all()
    # 9 vs 10, 12 vs 11, 12 vs 9
    assert raw.iloc[2:].tolist() == [-1.0, 1.0, 1.0]


def test_tsmom_scaled_mode_is_the_return_over_its_expected_std():
    closes = pd.Series(100.0 * np.exp(0.01 * np.arange(60)))  # constant 1% log return
    raw = tsmom_raw(closes, lookback=20, mode="scaled", vol_span=10)
    # sigma of a constant 1% zero-mean ewma is 1%; r_20 = 20%
    assert raw.iloc[-1] == pytest.approx(0.20 / (0.01 * math.sqrt(20)))


def test_tsmom_scaled_scalar_targets_mean_absolute_ten_on_a_random_walk():
    rng = np.random.default_rng(3)
    closes = pd.Series(100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, 30_000))))
    raw = tsmom_raw(closes, lookback=21, mode="scaled").iloc[200:]
    assert (raw * TSMOM_SCALED_SCALAR).abs().mean() == pytest.approx(10.0, rel=0.2)
    assert pytest.approx(10.0 / math.sqrt(2.0 / math.pi)) == TSMOM_SCALED_SCALAR


def test_tsmom_rejects_unknown_mode_and_bad_lookback():
    closes = pd.Series([1.0, 2.0, 3.0])
    with pytest.raises(ValueError):
        tsmom_raw(closes, 1, mode="nope")
    with pytest.raises(ValueError):
        tsmom_raw(closes, 0)


def test_tsmom_is_causal():
    closes = _random_walk(600).abs() + 1.0
    changed = closes.copy()
    changed.iloc[300:] *= 0.5
    for mode in ("sign", "scaled"):
        a = tsmom_raw(closes, 50, mode=mode)
        b = tsmom_raw(changed, 50, mode=mode)
        pd.testing.assert_series_equal(a.iloc[:300], b.iloc[:300])
