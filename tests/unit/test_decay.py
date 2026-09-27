"""The alpha-decay monitor (BL-47, roadmap 9.5.4): live results against
what the backtest promised."""

from __future__ import annotations

import math

import numpy as np
import pytest

from stonks.production.decay import (
    DecaySettings,
    evaluate_decay,
    expected_ir,
    rolling_ir,
    trailing_negative_days,
)


def test_rolling_ir_is_the_annualised_mean_over_std():
    r = [0.01, -0.005, 0.002, 0.004] * 20
    arr = np.array(r[-60:])
    want = arr.mean() / arr.std(ddof=1) * math.sqrt(252)
    assert rolling_ir(r, 60) == pytest.approx(want)


def test_rolling_ir_needs_a_full_window_and_some_risk():
    assert rolling_ir([0.01] * 10, 60) is None
    assert rolling_ir([0.0] * 80, 60) is None


def test_trailing_negative_days_counts_the_last_run_of_negative_irs():
    good = [0.01, 0.0] * 40  # IR > 0
    bad = [-0.01, 0.0] * 15  # pulls the rolling IR below zero
    assert trailing_negative_days(good, 20) == 0
    days = trailing_negative_days(good + bad, 20)
    assert 0 < days <= len(bad)


def test_expected_ir_prefers_the_oos_sharpe_then_the_benchmark_ir():
    assert expected_ir({"oos": {"sharpe_oos": 1.2}, "benchmark_relative": {}}) == 1.2
    assert expected_ir({"benchmark_relative": {"information_ratio": 0.4}}) == 0.4
    assert expected_ir({"oos": {"sharpe_oos": float("nan")}}) is None
    assert expected_ir({}) is None


def test_no_decay_without_history():
    check = evaluate_decay([0.001] * 10, 1.0, DecaySettings())
    assert not check.decayed and check.ir_short is None and check.reason is None


def test_decay_fires_when_the_short_ir_stays_below_zero_for_n_days():
    rng = np.random.default_rng(5)
    losing = list(rng.normal(-0.003, 0.01, 150))
    check = evaluate_decay(losing, None, DecaySettings(negative_days=20))
    assert check.decayed
    assert check.days_negative >= 20
    assert "below zero" in (check.reason or "")


def test_decay_fires_when_the_long_ir_falls_below_half_the_backtest():
    rng = np.random.default_rng(9)
    weak = list(rng.normal(0.0004, 0.01, 150))
    check = evaluate_decay(weak, 5.0, DecaySettings(negative_days=10_000))
    assert check.ir_long is not None and check.ir_long < 2.5
    assert check.decayed and "50%" in (check.reason or "")


def test_healthy_live_results_do_not_decay():
    rng = np.random.default_rng(2)
    strong = list(rng.normal(0.002, 0.01, 150))
    check = evaluate_decay(strong, 1.0, DecaySettings())
    assert not check.decayed
    assert check.ir_short is not None and check.ir_short > 0
