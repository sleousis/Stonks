"""Benchmark-relative statistics and beta attribution (BL-22, BL-23)."""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pytest

from stonks.backtest.benchmark import (
    BenchmarkCurve,
    BenchmarkedReport,
    attribute,
    benchmark_stats,
    normalize_spec,
    with_benchmark,
)
from stonks.backtest.report import compute_report


def _curve(returns, start=100.0):
    return list(start * np.cumprod(np.concatenate([[1.0], 1.0 + np.asarray(returns)])))


def _dates(n):
    d0 = date(2020, 1, 1)
    return [d0 + timedelta(days=i) for i in range(n)]


def _market(n=1500, seed=0):
    return np.random.default_rng(seed).normal(0.0004, 0.01, n)


# ---- attribution --------------------------------------------------------------


def test_attribute_recovers_beta_and_positive_alpha():
    rng = np.random.default_rng(1)
    r_m = rng.normal(0.0004, 0.01, 2000)
    r = 0.0002 + 1.5 * r_m + rng.normal(0.0, 0.002, 2000)
    att = attribute(r, r_m, periods_per_year=252)
    assert att.beta == pytest.approx(1.5, abs=0.03)
    assert att.alpha_annual == pytest.approx(0.0002 * 252, rel=0.35)
    assert att.alpha_tstat > 2.0
    assert 0.9 < att.r2 <= 1.0
    assert att.residual_sharpe > 0


def test_attribute_identical_series_has_beta_one_and_no_alpha():
    r_m = _market()
    att = attribute(r_m, r_m)
    assert att.beta == pytest.approx(1.0)
    assert att.alpha_annual == pytest.approx(0.0, abs=1e-12)
    assert att.alpha_tstat == 0.0
    assert att.r2 == pytest.approx(1.0)


def test_attribute_flat_benchmark_is_finite():
    r = _market()
    att = attribute(r, np.zeros_like(r))
    assert att.beta == 0.0
    assert att.alpha_annual == pytest.approx(float(np.mean(r)) * 252)
    assert math.isfinite(att.alpha_tstat)


def test_attribute_too_short_is_zero():
    att = attribute([0.01], [0.02])
    assert (att.beta, att.alpha_annual, att.alpha_tstat) == (0.0, 0.0, 0.0)


def test_attribute_rejects_mismatched_lengths():
    with pytest.raises(ValueError):
        attribute([0.1, 0.2, 0.3], [0.1, 0.2])


# ---- benchmark stats ---------------------------------------------------------------


def test_strategy_identical_to_benchmark_has_zero_ir_and_beta_one():
    r_m = _market()
    curve = _curve(r_m)
    stats = benchmark_stats(_dates(len(curve)), curve, curve, periods_per_year=252, name="B")
    assert stats.information_ratio == 0.0
    assert stats.tracking_error == pytest.approx(0.0, abs=1e-12)
    assert stats.beta == pytest.approx(1.0)
    assert stats.excess_cagr == pytest.approx(0.0, abs=1e-12)
    assert stats.up_capture == pytest.approx(1.0)
    assert stats.down_capture == pytest.approx(1.0)
    assert stats.correlation == pytest.approx(1.0)
    assert stats.name == "B"


def test_leveraged_copy_has_beta_two():
    r_m = _market()
    stats = benchmark_stats(
        _dates(len(r_m) + 1), _curve(2 * r_m), _curve(r_m), periods_per_year=252
    )
    assert stats.beta == pytest.approx(2.0, abs=1e-9)
    assert stats.up_capture == pytest.approx(2.0)
    assert stats.down_capture == pytest.approx(2.0)


def test_outperformer_has_positive_ir_and_excess_cagr():
    r_m = _market()
    rng = np.random.default_rng(3)
    r = r_m + 0.0005 + rng.normal(0, 0.001, r_m.size)
    dates = _dates(r_m.size + 1)
    stats = benchmark_stats(dates, _curve(r), _curve(r_m), periods_per_year=252)
    assert stats.information_ratio > 0
    assert stats.excess_cagr > 0
    assert stats.tracking_error > 0
    active = r - r_m
    assert stats.information_ratio == pytest.approx(
        active.mean() / active.std(ddof=1) * math.sqrt(252)
    )
    assert stats.tracking_error == pytest.approx(active.std(ddof=1) * math.sqrt(252))
    assert stats.benchmark_max_dd < 0
    assert stats.n_obs == r_m.size


def test_benchmark_stats_rejects_misaligned_curves():
    with pytest.raises(ValueError):
        benchmark_stats(_dates(3), [1.0, 1.1, 1.2], [1.0, 1.1], periods_per_year=252)


def test_benchmark_stats_empty_is_zero():
    stats = benchmark_stats([], [], [], periods_per_year=252)
    assert stats.n_obs == 0
    assert stats.information_ratio == 0.0


# ---- spec + attach ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("auto", "auto"),
        ("AUTO", "auto"),
        ("EW", "ew"),
        ("ew", "ew"),
        (" SPY.US ", "SPY.US"),
        ("none", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_spec(raw, expected):
    assert normalize_spec(raw) == expected


def test_with_benchmark_keeps_report_fields_and_adds_stats():
    r_m = _market(50)
    dates = _dates(51)
    report = compute_report("s", dates, _curve(2 * r_m))
    curve = BenchmarkCurve(
        name="EW", spec="ew", dates=tuple(dates), values=tuple(_curve(r_m, start=1.0))
    )
    out = with_benchmark(report, curve)
    assert isinstance(out, BenchmarkedReport)
    assert out.sharpe == report.sharpe and out.equity_curve == report.equity_curve
    assert out.benchmark is not None
    assert out.benchmark.stats.beta == pytest.approx(2.0)
    assert out.benchmark.curve is curve
    metrics = out.benchmark.metrics()
    assert metrics["beta"] == pytest.approx(2.0)
    assert all(isinstance(v, float) for v in metrics.values())


def test_with_benchmark_none_curve_sets_none():
    report = compute_report("s", _dates(3), [1.0, 1.1, 1.2])
    out = with_benchmark(report, None)
    assert out.benchmark is None
    assert out.cagr == report.cagr


def test_with_benchmark_rejects_other_dates():
    report = compute_report("s", _dates(3), [1.0, 1.1, 1.2])
    curve = BenchmarkCurve(name="X", spec="X", dates=tuple(_dates(4)[1:]), values=(1, 1, 1))
    with pytest.raises(ValueError, match="dates"):
        with_benchmark(report, curve)
