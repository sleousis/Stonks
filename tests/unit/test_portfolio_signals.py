"""Signal normalisation: raw strategy scores onto one comparable scale."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from stonks.features.cross_section import cs_zscore
from stonks.portfolio.signals import (
    DEFAULT_IC,
    FORECAST_CAP,
    SignalContext,
    estimate_forecast_scalar,
    normalize,
    normalizer_names,
)

# BuyAndHold's constant 1.0 next to a quality-value strategy's small scores
RAW = {
    "buy_and_hold": {"SPY": 1.0},
    "quality_value": {"A": 0.05, "B": 0.10, "C": 0.20, "D": 0.02},
}


def test_methods_are_registered() -> None:
    assert {"zscore", "rank", "forecast", "alpha"} <= set(normalizer_names())


def test_unknown_method_lists_valid_ones() -> None:
    with pytest.raises(ValueError, match="zscore"):
        normalize(RAW, "bogus")


def test_zscore_is_per_strategy_and_hand_checked() -> None:
    out = normalize(RAW, "zscore", long_only=False)
    vals = [0.05, 0.10, 0.20, 0.02]
    mean = sum(vals) / 4
    std = math.sqrt(sum((v - mean) ** 2 for v in vals) / 4)
    assert out["quality_value"]["C"] == pytest.approx((0.20 - mean) / std)
    assert out["quality_value"]["D"] < 0


def test_buy_and_hold_no_longer_dominates_after_zscore() -> None:
    out = normalize(RAW, "zscore")
    # sign-only conviction for a strategy with fewer than 3 names
    assert out["buy_and_hold"]["SPY"] == 1.0
    assert out["quality_value"]["C"] > out["buy_and_hold"]["SPY"]


def test_buy_and_hold_no_longer_dominates_after_forecast_scaling() -> None:
    ctx = SignalContext(forecast_scalars={"buy_and_hold": 10.0, "quality_value": 100.0})
    out = normalize(RAW, "forecast", context=ctx)
    assert out["buy_and_hold"]["SPY"] == pytest.approx(10.0)
    assert out["quality_value"]["C"] == pytest.approx(FORECAST_CAP)  # 0.20*100 capped at 20
    assert out["quality_value"]["B"] == pytest.approx(10.0)


def test_long_only_clips_negatives_to_zero() -> None:
    out = normalize(RAW, "zscore", long_only=True)
    assert out["quality_value"]["D"] == 0.0
    assert all(v >= 0 for s in out.values() for v in s.values())


def test_zscore_winsorises_outliers() -> None:
    raw = {"s": {f"T{i}": float(i) for i in range(30)} | {"OUT": 1000.0}}
    out = normalize(raw, "zscore", long_only=False)
    unwinsorised = cs_zscore(pd.Series(raw["s"]), winsor=None)["OUT"]
    assert unwinsorised > 5.0
    assert out["s"]["OUT"] <= 3.0 + 1e-6


def test_rank_gives_percentiles() -> None:
    out = normalize(RAW, "rank")
    assert out["quality_value"]["C"] == 1.0
    assert out["quality_value"]["D"] == 0.25


def test_forecast_without_stored_scalar_estimates_cross_sectionally() -> None:
    out = normalize(RAW, "forecast")
    qv = out["quality_value"]
    mean_abs = sum(abs(v) for v in qv.values()) / 4
    assert mean_abs == pytest.approx(10.0, rel=0.05)  # C is capped, so slightly below
    assert out["buy_and_hold"]["SPY"] == 10.0  # sign-only conviction on the forecast scale


def test_forecast_is_capped_both_ways() -> None:
    ctx = SignalContext(forecast_scalars={"s": 100.0})
    out = normalize({"s": {"X": -1.0, "Y": 1.0}}, "forecast", long_only=False, context=ctx)
    assert out["s"] == {"X": -FORECAST_CAP, "Y": FORECAST_CAP}


def test_estimate_forecast_scalar() -> None:
    assert estimate_forecast_scalar([1.0, -3.0, 2.0]) == pytest.approx(10.0 / 2.0)
    with pytest.raises(ValueError):
        estimate_forecast_scalar([0.0, 0.0])


def test_alpha_is_ic_times_sigma_times_z() -> None:
    raw = {"s": {"A": 1.0, "B": 2.0, "C": 3.0}}
    vols = {"A": 0.1, "B": 0.2, "C": 0.4}
    out = normalize(
        raw, "alpha", long_only=False, context=SignalContext(ics={"s": 0.05}, vols_annual=vols)
    )
    z_c = 1.0 / math.sqrt(2 / 3)
    assert out["s"]["C"] == pytest.approx(0.05 * 0.4 * z_c)
    assert out["s"]["B"] == pytest.approx(0.0)


def test_alpha_defaults_ic_and_drops_names_without_vol() -> None:
    raw = {"s": {"A": 1.0, "B": 2.0, "C": 3.0, "D": 4.0}}
    ctx = SignalContext(vols_annual={"A": 0.2, "B": 0.2, "C": 0.2})
    out = normalize(raw, "alpha", long_only=False, context=ctx)
    assert "D" not in out["s"]
    z_c = 1.0 / math.sqrt(2 / 3)
    assert out["s"]["C"] == pytest.approx(DEFAULT_IC * 0.2 * z_c)


def test_non_finite_scores_are_dropped() -> None:
    raw = {"s": {"A": 1.0, "B": float("nan"), "C": 2.0, "D": 3.0, "E": float("inf")}}
    out = normalize(raw, "zscore", long_only=False)
    assert set(out["s"]) == {"A", "C", "D"}


def test_empty_strategy_stays_empty() -> None:
    assert normalize({"s": {}}, "zscore") == {"s": {}}


def test_raw_passes_through() -> None:
    assert normalize(RAW, "raw") == RAW
