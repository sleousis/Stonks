"""The informational ``signal_ic`` survival test (BL-33)."""

from __future__ import annotations

import pytest

from stonks.lab.survival.registry import build_survival_test, survival_test_names
from tests.fixtures.signal_research import (
    FutureReturnSignal,
    StaticSignal,
    dataset_for,
    signal_lake,
)

TICKERS = [f"T{i:02d}" for i in range(12)]


@pytest.fixture(scope="module")
def lake():
    lake = signal_lake(":memory:", TICKERS, periods=260, seed=4)
    yield lake
    lake.close()


def test_registered():
    assert "signal_ic" in survival_test_names()


def test_always_passes_and_reports_ic_on_the_validation_window(lake):
    test = build_survival_test("signal_ic", {"horizons": [1, 5], "every_bars": 1, "max_workers": 1})
    ds = dataset_for(lake, TICKERS)
    report = test.run(FutureReturnSignal({"horizon": 1}), ds)
    assert report.test_id == "signal_ic"
    assert report.passed
    assert report.metrics["ic_mean_h1"] == pytest.approx(1.0)
    assert report.metrics["ic_estimate"] == pytest.approx(report.metrics["ic_mean_h5"])
    assert str(ds.val_window[0]) in report.notes
    # only validation-window dates were scored
    assert report.metrics["n_dates"] < 100


def test_full_window_option(lake):
    test = build_survival_test(
        "signal_ic", {"horizons": [1], "every_bars": 1, "window": "full", "max_workers": 1}
    )
    report = test.run(StaticSignal({}), dataset_for(lake, TICKERS))
    assert report.metrics["n_dates"] == 260


def test_small_universe_is_na_but_passes(lake):
    test = build_survival_test("signal_ic", {"max_workers": 1})
    report = test.run(StaticSignal({}), dataset_for(lake, TICKERS[:5]))
    assert report.passed
    assert report.notes.startswith("n/a")


def test_rejects_bad_options():
    with pytest.raises(ValueError):
        build_survival_test("signal_ic", {"horizons": []})
    with pytest.raises(ValueError):
        build_survival_test("signal_ic", {"every_bars": 0})
