"""tune_and_fit + the runner handing its tuner/objective/budget to survival
tests that re-tune (walk-forward, re-tuning MCPT)."""

from __future__ import annotations

from datetime import date

from stonks.core.protocols import SurvivalReport, TunerResult
from stonks.lab.dataset import LabDataset
from stonks.lab.runner import LabRunner
from stonks.lab.survival.base import SurvivalSuite, TuningSetup
from stonks.lab.tuning.base import tune_and_fit
from stonks.strategies.examples.buy_and_hold import BuyAndHold


class _FixedTuner:
    def __init__(self, allocation: float) -> None:
        self.allocation = allocation
        self.calls: list[tuple[object, int]] = []

    def tune(self, strategy_cls, param_space, objective, dataset, budget):
        self.calls.append((dataset, budget))
        params = {"ticker": "X.US", "allocation": self.allocation}
        return TunerResult(best_params=params, best_score=1.5, history=[(params, 1.5)])


class _Objective:
    name = "fake"
    direction = "maximize"

    def score(self, strategy, dataset):
        return 0.0


class _BindingTest:
    id = "binding"

    def __init__(self) -> None:
        self.bound: TuningSetup | None = None

    def bind_tuning(self, setup: TuningSetup) -> None:
        self.bound = setup

    def run(self, strategy, context):
        return SurvivalReport(test_id=self.id, passed=True, metrics={})


class _PlainTest:
    id = "plain"

    def run(self, strategy, context):
        return SurvivalReport(test_id=self.id, passed=True, metrics={})


def _ds():
    return LabDataset(lake=None, universe=["X.US"], start=date(2024, 1, 1), end=date(2024, 6, 1))


def test_tune_and_fit_builds_the_best_strategy_and_fits_it_on_the_dataset():
    fitted: list[object] = []

    class _Tracked(BuyAndHold):
        def fit(self, dataset):
            fitted.append(dataset)

    tuner = _FixedTuner(allocation=0.4)
    ds = _ds()
    strategy, result = tune_and_fit(_Tracked, ds, TuningSetup(tuner, _Objective(), budget=3))
    assert isinstance(strategy, _Tracked)
    assert strategy.params["allocation"] == 0.4
    assert result.best_score == 1.5
    assert tuner.calls == [(ds, 3)]
    assert fitted == [ds]


def test_runner_binds_its_tuning_setup_to_tests_that_accept_one():
    tuner, objective = _FixedTuner(0.5), _Objective()
    binding, plain = _BindingTest(), _PlainTest()
    LabRunner(tuner, objective, SurvivalSuite([binding, plain]), budget=7).run(BuyAndHold, _ds())
    assert binding.bound == TuningSetup(tuner=tuner, objective=objective, budget=7)
