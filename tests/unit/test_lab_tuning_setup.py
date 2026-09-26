"""tune_and_fit + the runner handing its tuner/objective/budget to survival
tests that re-tune (walk-forward, re-tuning MCPT)."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.core.protocols import SurvivalReport, TunerResult
from stonks.lab.dataset import LabDataset
from stonks.lab.runner import LabRunner
from stonks.lab.survival.base import SurvivalSuite, TuningSetup
from stonks.lab.tuning.base import fixed_params_of, tune_and_fit
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from stonks.strategies.examples.momentum import Momentum


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


# ---- fixed (non-tunable) params ------------------------------------------------


class _SpaceRecordingTuner:
    """Returns the defaults of the space it is handed, like a tuner whose
    grid collapsed to the defaults."""

    def __init__(self) -> None:
        self.spaces: list[list] = []

    def tune(self, strategy_cls, param_space, objective, dataset, budget):
        self.spaces.append(list(param_space))
        params = {s.name: s.default for s in param_space}
        return TunerResult(best_params=params, best_score=0.0, history=[(params, 0.0)])


def test_fixed_params_of_keeps_only_the_non_tunable_params():
    assert fixed_params_of(BuyAndHold({"ticker": "Y.US", "allocation": 0.3})) == {
        "ticker": "Y.US",
        "allocation": 0.3,
    }
    # Momentum: lookback_days / threshold are tunable, allocation and
    # skip_days are not (an old-style param set pins skip_days to 0)
    assert fixed_params_of(Momentum({"lookback_days": 5, "allocation": 0.5})) == {
        "allocation": 0.5,
        "skip_days": 0,
    }


def test_tune_and_fit_keeps_fixed_params_in_the_space_and_the_result():
    tuner = _SpaceRecordingTuner()
    setup = TuningSetup(tuner, _Objective(), budget=1)
    strategy, result = tune_and_fit(BuyAndHold, _ds(), setup, fixed_params={"ticker": "Y.US"})
    (space,) = tuner.spaces
    by_name = {s.name: s for s in space}
    assert by_name["ticker"].default == "Y.US"
    assert by_name["allocation"].default == 1.0  # params not fixed keep their spec
    assert strategy.params["ticker"] == "Y.US"
    assert result.best_params["ticker"] == "Y.US"


def test_fixed_params_win_over_whatever_the_tuner_returns():
    # _FixedTuner ignores the space and returns ticker X.US
    setup = TuningSetup(_FixedTuner(0.4), _Objective(), budget=1)
    strategy, _ = tune_and_fit(BuyAndHold, _ds(), setup, fixed_params={"ticker": "Y.US"})
    assert strategy.params == {"ticker": "Y.US", "allocation": 0.4}


def test_fixing_a_tunable_param_pins_it():
    tuner = _SpaceRecordingTuner()
    setup = TuningSetup(tuner, _Objective(), budget=1)
    strategy, _ = tune_and_fit(Momentum, _ds(), setup, fixed_params={"lookback_days": 7})
    by_name = {s.name: s for s in tuner.spaces[0]}
    assert by_name["lookback_days"].tunable is False
    assert by_name["threshold"].tunable is True
    assert strategy.params["lookback_days"] == 7


def test_unknown_fixed_param_is_rejected():
    setup = TuningSetup(_FixedTuner(0.4), _Objective(), budget=1)
    with pytest.raises(ValueError, match="nope"):
        tune_and_fit(BuyAndHold, _ds(), setup, fixed_params={"nope": 1})


def test_runner_passes_fixed_params_to_tuning():
    tuner = _SpaceRecordingTuner()
    result = LabRunner(tuner, _Objective(), SurvivalSuite([]), budget=1).run(
        BuyAndHold, _ds(), fixed_params={"ticker": "Y.US"}
    )
    assert result.strategy.params["ticker"] == "Y.US"
    assert result.best_params["ticker"] == "Y.US"
