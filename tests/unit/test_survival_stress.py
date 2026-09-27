"""Stress survival test: block bootstrap and GARCH-t filtered historical
simulation of the validation window (BL-48)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stonks.lab.dataset import LabDataset
from stonks.lab.survival.registry import build_survival_test, survival_test_names
from stonks.lab.survival.stress import resample_window, stationary_bootstrap
from tests.unit.nt888_helpers import write_bars
from tests.unit.test_survival_crisis import HoldAll

DATES = pd.bdate_range("2021-01-01", periods=300)


def _bars(closes):
    opens = np.concatenate([[closes[0]], closes[:-1]]) * 1.001
    return pd.DataFrame(
        {
            "ticker": "X",
            "timestamp": DATES[: len(closes)],
            "open": opens,
            "high": np.maximum(opens, closes) * 1.01,
            "low": np.minimum(opens, closes) * 0.99,
            "close": closes,
            "adj_close": closes,
            "volume": np.arange(len(closes), dtype=float),
        }
    )


# ---- building blocks ----------------------------------------------------------------


def test_stationary_bootstrap_is_seeded_and_runs_in_blocks():
    a = stationary_bootstrap(100, 5000, 20.0, np.random.default_rng(1))
    b = stationary_bootstrap(100, 5000, 20.0, np.random.default_rng(1))
    assert np.array_equal(a, b)
    assert a.min() >= 0 and a.max() < 100
    runs = 1 + np.count_nonzero(np.diff(a) != 1) - np.count_nonzero(np.diff(a) == -99)
    assert 5000 / runs == pytest.approx(20.0, rel=0.2)
    assert np.array_equal(
        stationary_bootstrap(10, 5, 1.0, np.random.default_rng(0)) >= 0, [True] * 5
    )
    with pytest.raises(ValueError):
        stationary_bootstrap(0, 5, 2.0, np.random.default_rng(0))
    with pytest.raises(ValueError):
        stationary_bootstrap(5, 5, 0.5, np.random.default_rng(0))


def test_resample_keeps_history_and_copies_the_source_bar_shape():
    closes = 100 * np.exp(np.cumsum(np.random.default_rng(0).normal(0, 0.01, 50)))
    bars = _bars(closes)
    src = np.full(20, 10)
    out = resample_window(bars, 30, src)
    pd.testing.assert_frame_equal(out.iloc[:30], bars.iloc[:30])
    body = np.log(bars["close"][10] / bars["open"][10])
    np.testing.assert_allclose(np.log(out["close"][30:] / out["open"][30:]), body)
    gap = np.log(bars["open"][10] / bars["close"][9])
    assert np.log(out["open"][30] / out["close"][29]) == pytest.approx(gap)
    assert (out["high"] >= out[["open", "close"]].max(axis=1) - 1e-9).all()
    assert (out["low"] <= out[["open", "close"]].min(axis=1) + 1e-9).all()
    assert (out["adj_close"] == out["close"]).all()
    assert (out["volume"][30:] == 10.0).all()


def test_resample_with_given_returns_and_from_the_first_row():
    bars = _bars(np.full(10, 100.0))
    out = resample_window(bars, 5, np.full(5, 3), returns=np.full(5, 0.01))
    np.testing.assert_allclose(np.diff(np.log(out["close"][4:])), 0.01)
    kept_first = resample_window(bars, 0, np.arange(10))
    assert kept_first["close"][0] == bars["close"][0]
    with pytest.raises(ValueError):
        resample_window(bars, 5, np.arange(3))
    assert resample_window(bars, 10, np.array([], dtype=int)).equals(bars.reset_index(drop=True))


# ---- the test --------------------------------------------------------------------------


@pytest.fixture
def stress_lake(lake):
    rng = np.random.default_rng(5)
    write_bars(lake, "UP.US", DATES, 100 * np.exp(np.cumsum(rng.normal(0.004, 0.006, 300))))
    write_bars(lake, "DOWN.US", DATES, 100 * np.exp(np.cumsum(rng.normal(-0.004, 0.03, 300))))
    return lake


def _ds(lake, ticker):
    return LabDataset(
        lake=lake,
        universe=[ticker],
        start=DATES[0].date(),
        end=DATES[-1].date(),
        benchmark="none",
    )


def test_registered():
    assert "stress" in survival_test_names()


def test_a_steady_winner_survives_the_simulated_windows(stress_lake):
    test = build_survival_test("stress", {"n_paths": 8, "max_workers": 1})
    report = test.run(HoldAll({}), _ds(stress_lake, "UP.US"))
    assert report.passed, report.notes
    assert report.metrics["n_paths"] == 8
    assert report.metrics["sharpe_p5"] > 1.0


def test_a_volatile_loser_fails_and_the_bootstrap_is_seeded(stress_lake):
    test = build_survival_test("stress", {"n_paths": 8, "max_workers": 1})
    a = test.run(HoldAll({}), _ds(stress_lake, "DOWN.US"))
    b = test.run(HoldAll({}), _ds(stress_lake, "DOWN.US"))
    assert not a.passed
    assert a.metrics == b.metrics
    assert a.metrics["max_dd_p95"] <= a.metrics["max_dd_median"]


def test_garch_filtered_simulation_and_source_val(stress_lake):
    test = build_survival_test(
        "stress", {"n_paths": 4, "method": "garch_fhs", "source": "val", "max_workers": 1}
    )
    report = test.run(HoldAll({}), _ds(stress_lake, "UP.US"))
    assert report.metrics["n_paths"] == 4
    assert np.isfinite(report.metrics["sharpe_median"])


def test_empty_window_fails(lake):
    write_bars(lake, "OLD.US", DATES[:50], np.full(50, 100.0))
    report = build_survival_test("stress", {"n_paths": 2, "max_workers": 1}).run(
        HoldAll({}), _ds(lake, "OLD.US")
    )
    assert not report.passed and "insufficient data" in report.notes
