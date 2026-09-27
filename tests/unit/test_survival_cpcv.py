"""CPCV survival test (BL-45)."""

from __future__ import annotations

from datetime import date
from typing import ClassVar

import pytest

from stonks.lab.objectives import SharpeObjective
from stonks.lab.parallel import ParallelSettings
from stonks.lab.survival.base import TuningSetup
from stonks.lab.survival.registry import (
    build_survival_test,
    preset_options,
    resolve_preset,
    survival_test_names,
)
from stonks.lab.tuning.grid import GridTuner
from tests.fixtures.signal_research import WeekdaySignal, dataset_for, signal_lake

TICKERS = ["WA", "WB"]


@pytest.fixture(scope="module")
def edge_lake():
    lake = signal_lake(
        ":memory:", TICKERS, periods=240, seed=21, weekday_edge={"WA": (2, 0.012), "WB": (2, 0.012)}
    )
    yield lake
    lake.close()


@pytest.fixture(scope="module")
def losing_lake():
    lake = signal_lake(
        ":memory:",
        TICKERS,
        periods=240,
        seed=22,
        weekday_edge={"WA": (2, -0.012), "WB": (2, -0.012)},
    )
    yield lake
    lake.close()


class FitRecorder(WeekdaySignal):
    """Weekday signal that records every training window it was fitted on."""

    id = "fit_recorder"
    label_horizon_bars = 2
    fits: ClassVar[list[tuple[tuple[date, date], ...]]] = []

    def fit(self, dataset):
        type(self).fits.append(tuple(dataset.train_windows))


def test_registered_and_in_the_promotion_preset():
    assert "cpcv" in survival_test_names()
    assert "cpcv" in resolve_preset("promotion")
    assert preset_options("promotion")["cpcv"]["n_groups"] == 6


def test_planted_edge_passes_on_every_path(edge_lake):
    test = build_survival_test("cpcv", {"max_workers": 1})
    report = test.run(WeekdaySignal({"weekday": 2}), dataset_for(edge_lake, TICKERS))
    m = report.metrics
    assert report.passed, report.notes
    assert m["n_splits"] == 15 and m["n_paths"] == 5
    assert m["positive_share"] == 1.0
    assert m["psr0_pooled"] >= 0.9
    assert m["retuned"] == 0.0
    assert {f"sharpe_path_{i}" for i in range(5)} <= set(m)


def test_identical_paths_pool_to_one_path_psr(edge_lake):
    """BE-08: every held-out day sits in every path, so a strategy with no
    fit (identical paths) pools to exactly one path's PSR."""
    test = build_survival_test("cpcv", {"max_workers": 1})
    report = test.run(WeekdaySignal({"weekday": 2}), dataset_for(edge_lake, TICKERS))
    m = report.metrics
    assert m["psr0_pooled"] == pytest.approx(m["psr0_path_0"])
    assert m["psr0_path_0"] == pytest.approx(m["psr0_path_4"])


def test_a_path_psr_of_0_8_does_not_pool_past_the_gate():
    """BE-08: five copies of one path must not shrink the PSR's error."""
    import numpy as np

    from stonks.lab.survival.cpcv import pooled_psr
    from stonks.lab.survival.walk_forward import psr0

    rng = np.random.default_rng(1)
    x = rng.normal(0.0, 0.01, 500)
    x = x - x.mean() + 0.00038
    single = psr0(x)
    assert 0.75 < single < 0.85
    assert pooled_psr([x] * 5) == pytest.approx(single)
    assert pooled_psr([x] * 5) < 0.9


def test_losing_edge_fails(losing_lake):
    test = build_survival_test("cpcv", {"max_workers": 1})
    report = test.run(WeekdaySignal({"weekday": 2}), dataset_for(losing_lake, TICKERS))
    assert not report.passed
    assert "positive paths" in report.notes


def test_every_fit_is_purged_away_from_its_test_groups(edge_lake):
    FitRecorder.fits = []
    test = build_survival_test("cpcv", {"n_groups": 4, "n_test_groups": 1, "max_workers": 1})
    report = test.run(FitRecorder({"weekday": 2}), dataset_for(edge_lake, TICKERS))
    assert report.metrics["n_splits"] == 4 and report.metrics["n_paths"] == 1
    assert report.metrics["purge_bars"] == 2
    assert len(FitRecorder.fits) == 4
    # the middle groups train on data either side of the test group
    assert [len(f) for f in FitRecorder.fits] == [1, 2, 2, 1]
    first, second = FitRecorder.fits[1]
    assert (second[0] - first[1]).days > 60  # a whole test group plus the purge


def test_retunes_each_split_when_a_setup_is_bound(edge_lake):
    test = build_survival_test("cpcv", {"n_groups": 4, "n_test_groups": 2, "max_workers": 1})
    test.bind_tuning(
        TuningSetup(
            tuner=GridTuner(grid_size=5, parallel=ParallelSettings(max_workers=1)),
            objective=SharpeObjective(),
            budget=5,
        )
    )
    report = test.run(WeekdaySignal({"weekday": 0}), dataset_for(edge_lake, TICKERS))
    assert report.metrics["retuned"] == 1.0
    # the tuner finds the Wednesday edge on the training groups
    assert report.metrics["positive_share"] >= 0.6


def test_too_little_data_fails(edge_lake):
    ds = dataset_for(edge_lake, TICKERS)
    short = ds.__class__(lake=edge_lake, universe=TICKERS, start=ds.start, end=date(2023, 1, 9))
    report = build_survival_test("cpcv", {"max_workers": 1}).run(
        WeekdaySignal({"weekday": 2}), short
    )
    assert not report.passed and "insufficient data" in report.notes


def test_bad_options_are_rejected():
    with pytest.raises(ValueError):
        build_survival_test("cpcv", {"n_groups": 4, "n_test_groups": 4})
