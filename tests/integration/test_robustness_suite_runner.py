"""The BL-17..BL-19 tests run under LabRunner, built from the registry."""

from __future__ import annotations

from stonks.lab.objectives import SharpeObjective
from stonks.lab.runner import LabRunner
from stonks.lab.survival.base import SurvivalSuite
from stonks.lab.survival.registry import build_survival_test
from stonks.lab.tuning.grid import GridTuner
from tests.fixtures.robustness_lab import FlipFlop, dataset_for, trend_lake

_IDS = ("mc_trades", "cost_stress", "plateau", "cross_instrument")


def test_runner_binds_and_runs_every_robustness_test():
    lake = trend_lake(":memory:", dict.fromkeys(("A.US", "B.US", "C.US"), (0.002, 0.004)))
    try:
        suite = SurvivalSuite(
            [build_survival_test(t, {"max_workers": 1} if t != "mc_trades" else {}) for t in _IDS]
        )
        runner = LabRunner(GridTuner(grid_size=3, seed=11), SharpeObjective(), suite, budget=3)
        result = runner.run(FlipFlop, dataset_for(lake, ["A.US", "B.US", "C.US"]))
    finally:
        lake.close()
    reports = {r.test_id: r for r in result.survival_reports}
    assert set(reports) == set(_IDS)
    assert "seed=11" in reports["mc_trades"].notes  # the run's root seed
    assert reports["plateau"].metrics["n_trials"] == 3  # the ledger reached the test
    assert reports["plateau"].metrics["n_neighbours"] >= 1
    assert reports["cross_instrument"].metrics["n_tickers"] == 3
    assert "sharpe_2x" in reports["cost_stress"].metrics
