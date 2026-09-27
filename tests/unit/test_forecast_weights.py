"""Carver-style forecast weights net of costs (roadmap 22.7): the estimator
seam, the speed limit and the fit (P12: training data only, P19: costs)."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from stonks.features.forecast_weights import (
    ForecastWeightEstimator,
    WeightInput,
    fit_forecast_weights,
    forecast_turnover,
    get_weight_estimator,
    rule_net_returns,
    weight_estimator_names,
)

PPY = 252.0


def _returns(n: int = 1500, seed: int = 0) -> pd.DataFrame:
    """Three rules: A and B almost the same stream, C independent."""
    rng = np.random.default_rng(seed)
    a = rng.normal(0.0004, 0.01, n)
    b = a + rng.normal(0.0, 0.001, n)
    c = rng.normal(0.0004, 0.01, n)
    return pd.DataFrame({"a": a, "b": b, "c": c})


# ---- the seam ------------------------------------------------------------------------


def test_registry_lists_the_estimators():
    assert {"equal", "handcraft", "bootstrap"} <= set(weight_estimator_names())
    assert isinstance(get_weight_estimator("equal"), ForecastWeightEstimator)


def test_unknown_estimator_lists_the_choices():
    with pytest.raises(ValueError, match="handcraft"):
        get_weight_estimator("nope")


def test_equal_weights():
    w = get_weight_estimator("equal").estimate(WeightInput(_returns(), {}))
    assert w == pytest.approx({"a": 1 / 3, "b": 1 / 3, "c": 1 / 3})


def test_handcraft_splits_by_correlation_groups():
    w = get_weight_estimator("handcraft").estimate(WeightInput(_returns(), {}))
    # {a, b} form one group, c the other: half each, then halves inside
    assert w["c"] == pytest.approx(0.5, abs=0.01)
    assert w["a"] == pytest.approx(0.25, abs=0.01)


def test_handcraft_weights_uncorrelated_rules_equally():
    rng = np.random.default_rng(9)
    frame = pd.DataFrame(rng.normal(0.0, 0.01, (2000, 4)), columns=list("wxyz"))
    w = get_weight_estimator("handcraft").estimate(WeightInput(frame, {}))
    assert w == pytest.approx(dict.fromkeys("wxyz", 0.25), abs=0.02)
    assert sum(w.values()) == pytest.approx(1.0)


def test_handcraft_moves_weight_to_the_cheaper_rule():
    costs = {"a": 0.0, "b": 0.10, "c": 0.05}
    w = get_weight_estimator("handcraft").estimate(WeightInput(_returns(), costs))
    assert w["a"] > w["b"]
    assert sum(w.values()) == pytest.approx(1.0)
    assert min(w.values()) >= 0


def test_handcraft_single_rule_gets_everything():
    w = get_weight_estimator("handcraft").estimate(WeightInput(_returns()[["a"]], {}))
    assert w == {"a": 1.0}


def test_bootstrap_is_seeded_and_prefers_the_better_net_stream():
    rng = np.random.default_rng(3)
    n = 1500
    good = rng.normal(0.002, 0.01, n)
    bad = rng.normal(-0.001, 0.01, n)
    frame = pd.DataFrame({"good": good, "bad": bad})
    est = get_weight_estimator("bootstrap", draws=40, seed=1)
    w1 = est.estimate(WeightInput(frame, {}))
    w2 = get_weight_estimator("bootstrap", draws=40, seed=1).estimate(WeightInput(frame, {}))
    assert w1 == w2
    assert w1["good"] > 0.8
    assert sum(w1.values()) == pytest.approx(1.0)
    assert min(w1.values()) >= 0


# ---- building blocks -------------------------------------------------------------------


def test_turnover_of_a_flat_and_a_flipping_forecast():
    idx = pd.RangeIndex(300)
    flat = pd.Series(10.0, index=idx)
    flip = pd.Series([10.0 if i % 2 else -10.0 for i in idx], index=idx)
    assert forecast_turnover(flat, PPY) == 0.0
    # |delta f| / 10 = 2 every bar
    assert forecast_turnover(flip, PPY) == pytest.approx(2 * PPY)


def test_net_returns_pay_for_each_change_in_position():
    closes = pd.Series([100.0, 101.0, 99.0, 100.0, 102.0])
    sigma = pd.Series(0.01, index=closes.index)
    forecasts = pd.DataFrame({"r": [10.0, 10.0, -10.0, -10.0, 20.0]})
    net = rule_net_returns(forecasts, closes, sigma, cost=0.001)
    ret = closes.pct_change()
    pos = forecasts["r"] / 10.0 / sigma
    expected = pos.shift(1) * ret - pos.diff().abs() * 0.001
    pd.testing.assert_series_equal(net["r"], expected, check_names=False)


# ---- the fit -------------------------------------------------------------------------


def _market(n: int = 900, seed: int = 5, drift: float = 0.0005, vol: float = 0.01):
    rng = np.random.default_rng(seed)
    closes = pd.Series(50.0 * np.exp(np.cumsum(drift + rng.normal(0.0, vol, n))))
    fast = pd.Series(rng.normal(0.0, 10.0, n))  # a jumpy rule: high turnover
    slow = pd.Series(10.0 * np.sign(np.sin(np.arange(n) / 80.0)) + 0.1)  # rare flips
    return closes, pd.DataFrame({"fast": fast, "slow": slow})


def test_speed_limit_drops_the_rule_that_costs_too_much():
    closes, raw = _market()
    fit = fit_forecast_weights(
        raw,
        closes,
        fixed_scalars={"fast": 1.0, "slow": 1.0},
        cost=0.002,
        estimator=get_weight_estimator("equal"),
        max_cost_sr=0.13,
        periods_per_year=PPY,
    )
    rules = {r.name: r for r in fit.rules}
    assert fit.status == "ok"
    assert rules["fast"].dropped and "speed limit" in rules["fast"].reason
    assert not rules["slow"].dropped
    assert rules["fast"].weight == 0.0
    assert rules["slow"].weight == pytest.approx(1.0)
    assert rules["fast"].cost_sr > 0.13 >= rules["slow"].cost_sr
    assert fit.weights == {"slow": pytest.approx(1.0)}
    assert fit.fdm == 1.0  # a single kept rule


def test_cost_sr_is_turnover_times_cost_over_annual_vol():
    closes, raw = _market()
    fit = fit_forecast_weights(
        raw,
        closes,
        fixed_scalars={"fast": 1.0, "slow": 1.0},
        cost=0.001,
        estimator=get_weight_estimator("equal"),
        max_cost_sr=10.0,
        periods_per_year=PPY,
    )
    rule = next(r for r in fit.rules if r.name == "slow")
    assert rule.cost_sr == pytest.approx(rule.turnover * 0.001 / fit.sigma_annual)
    assert fit.sigma_annual == pytest.approx(0.01 * math.sqrt(PPY), rel=0.2)


def test_every_rule_too_costly():
    closes, raw = _market()
    fit = fit_forecast_weights(
        raw,
        closes,
        fixed_scalars={"fast": 1.0, "slow": 1.0},
        cost=0.05,
        estimator=get_weight_estimator("equal"),
        max_cost_sr=0.13,
        periods_per_year=PPY,
    )
    assert fit.status == "too_costly"
    assert fit.weights == {}
    assert all(r.dropped for r in fit.rules)


def test_short_history_is_a_warm_up_with_equal_weights():
    closes, raw = _market(n=120)
    fit = fit_forecast_weights(
        raw,
        closes,
        fixed_scalars={"fast": 1.0, "slow": 1.0},
        cost=0.05,
        estimator=get_weight_estimator("handcraft"),
        max_cost_sr=0.13,
        periods_per_year=PPY,
        fdm_fallback=1.1,
    )
    assert fit.status == "warmup"
    assert fit.weights == {"fast": 0.5, "slow": 0.5}
    assert fit.fdm == 1.1
    assert not any(r.dropped for r in fit.rules)


def test_estimated_scalars_and_fdm_come_from_the_training_rows():
    closes, raw = _market()
    fit = fit_forecast_weights(
        raw * 3.0,
        closes,
        fixed_scalars={"fast": 1.0, "slow": 1.0},
        cost=0.0,
        estimator=get_weight_estimator("equal"),
        max_cost_sr=0.13,
        periods_per_year=PPY,
        scalar_mode="estimate",
        fdm_mode="estimate",
    )
    scalars = {r.name: r.scalar for r in fit.rules}
    assert scalars["fast"] == pytest.approx(10.0 / (raw["fast"] * 3.0).abs().mean(), rel=1e-6)
    # two nearly uncorrelated rules: FDM close to sqrt(2)
    assert 1.2 < fit.fdm <= 2.5
