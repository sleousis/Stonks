"""PlateauTest on real backtests (BL-19)."""

from __future__ import annotations

from stonks.lab.objectives import SharpeObjective
from stonks.lab.survival.base import TuningSetup
from stonks.lab.survival.plateau import PlateauTest
from tests.fixtures.robustness_lab import (
    FixedTuner,
    FlipFlop,
    PeakTrader,
    dataset_for,
    trend_lake,
)


def _test(**kw) -> PlateauTest:
    test = PlateauTest(**kw)
    test.bind_tuning(TuningSetup(tuner=FixedTuner(), objective=SharpeObjective(), budget=4))
    return test


def test_peak_in_parameter_space_fails_in_and_out_of_sample():
    lake = trend_lake(":memory:", {"A.US": (0.003, 0.008)})
    try:
        report = _test(max_workers=1).run(PeakTrader({"a": 10}), dataset_for(lake, ["A.US"]))
    finally:
        lake.close()
    assert not report.passed
    assert report.metrics["best_oos_sharpe"] > 0
    assert report.metrics["neighbour_oos_sharpe_median"] == 0.0
    assert "train ratio" in report.notes
    assert "OOS Sharpe" in report.notes


def test_result_does_not_depend_on_worker_count(tmp_path):
    lake = trend_lake(tmp_path / "lake.duckdb", {"A.US": (0.002, 0.01), "B.US": (0.001, 0.01)})
    try:
        ds = dataset_for(lake, ["A.US", "B.US"])
        serial = _test(max_workers=1).run(FlipFlop({"hold_bars": 10}), ds)
        pooled = _test(max_workers=2).run(FlipFlop({"hold_bars": 10}), ds)
    finally:
        lake.close()
    assert serial.metrics["n_neighbours"] == 2
    assert serial == pooled
