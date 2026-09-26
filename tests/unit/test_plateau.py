"""Parameter plateau test (BL-19)."""

from __future__ import annotations

import pytest

from stonks.lab.objectives import SharpeObjective
from stonks.lab.survival.base import TuningSetup
from stonks.lab.survival.plateau import PlateauOptions, PlateauTest, plateau_neighbours
from stonks.lab.survival.registry import build_survival_test, survival_test_names
from stonks.lab.trials import LabRunContext, TrialRecord
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from tests.fixtures.robustness_lab import (
    FixedTuner,
    SurfaceObjective,
    SurfaceStrategy,
    dataset_for,
    trend_lake,
)


@pytest.fixture
def ds():
    lake = trend_lake(":memory:", {"A.US": (0.001, 0.01)}, periods=120)
    yield dataset_for(lake, ["A.US"])
    lake.close()


def _setup(objective=None, fixed=None) -> TuningSetup:
    return TuningSetup(
        tuner=FixedTuner(),
        objective=objective or SurfaceObjective(),
        budget=10,
        fixed_params=dict(fixed or {}),
    )


def _ctx(setup: TuningSetup, scores=()) -> LabRunContext:
    trials = [TrialRecord(trial_index=i, params={}, score=s) for i, s in enumerate(scores)]
    return LabRunContext(
        setup=setup,
        run_id="r1",
        ledger=None,
        trials=trials,
        trial_matrix=None,
        n_trials_run=len(trials),
        n_trials_class=len(trials),
    )


def _test(setup=None, scores=(), **kw) -> PlateauTest:
    test = PlateauTest(max_workers=1, **kw)
    test.bind_run(_ctx(setup or _setup(), scores))
    return test


def test_registered_under_its_id():
    assert "plateau" in survival_test_names()
    assert isinstance(build_survival_test("plateau", {"step": 0.1}), PlateauTest)


def test_option_bounds_follow_the_spec():
    assert PlateauOptions().step == 0.15
    assert PlateauOptions().min_train_ratio == 0.7
    with pytest.raises(ValueError):
        PlateauOptions(step=0.5)
    with pytest.raises(ValueError):
        PlateauOptions(min_train_ratio=0.95)


def test_neighbours_step_each_numeric_param_and_skip_categoricals():
    best = {"a": 10, "b": 0.5, "mode": "x", "flag": False, "shape": "smooth"}
    got = plateau_neighbours(SurfaceStrategy.parameter_spec(), best, step=0.15)
    assert [(name, value) for name, value, _ in got] == [
        ("a", 7),
        ("a", 13),
        ("b", pytest.approx(0.35)),
        ("b", pytest.approx(0.65)),
    ]
    for name, value, params in got:
        assert params == {**best, name: value}


def test_neighbours_clip_at_bounds_and_skip_pinned_params():
    best = {"a": 20, "b": 0.5, "mode": "x", "flag": False, "shape": "smooth"}
    got = plateau_neighbours(SurfaceStrategy.parameter_spec(), best, step=0.15, pinned={"b"})
    assert [(n, v) for n, v, _ in got] == [("a", 17)]


def test_small_int_ranges_still_move_one_step():
    from stonks.core.params import ParameterSpec

    space = [ParameterSpec(name="k", kind="int", default=2, bounds=(1, 4))]
    got = plateau_neighbours(space, {"k": 2}, step=0.05)
    assert [(n, v) for n, v, _ in got] == [("k", 1), ("k", 3)]


def test_smooth_surface_passes(ds):
    report = _test().run(SurfaceStrategy({"a": 10, "b": 0.5}), ds)
    assert report.passed, report.notes
    assert report.metrics["n_neighbours"] == 4
    assert report.metrics["train_ratio"] > 0.9
    assert report.metrics["best_train_score"] == pytest.approx(1.0)


def test_spike_fails(ds):
    strategy = SurfaceStrategy({"a": 10, "b": 0.5, "shape": "spike"})
    report = _test().run(strategy, ds)
    assert not report.passed
    assert report.metrics["train_ratio"] == pytest.approx(0.05)
    assert "train ratio" in report.notes


def test_non_positive_best_score_fails_with_a_note(ds):
    report = _test().run(SurfaceStrategy({"shape": "negative"}), ds)
    assert not report.passed
    assert "not positive" in report.notes


def test_no_tunable_numeric_params_is_insufficient_data(ds):
    report = _test(_setup(SharpeObjective())).run(BuyAndHold({"ticker": "A.US"}), ds)
    assert not report.passed
    assert "insufficient data" in report.notes
    assert report.metrics["n_neighbours"] == 0


def test_every_param_pinned_is_insufficient_data(ds):
    setup = _setup(fixed={"a": 10, "b": 0.5})
    report = _test(setup).run(SurfaceStrategy({"a": 10, "b": 0.5}), ds)
    assert not report.passed
    assert "insufficient data" in report.notes


def test_reports_the_ehlers_ratio_and_share_of_profitable_trials(ds):
    test = _test(scores=[1.0, 0.5, 0.4, -0.2, float("nan")])
    report = test.run(SurfaceStrategy({"a": 10, "b": 0.5}), ds)
    assert report.metrics["robustness_ratio"] == pytest.approx(0.45)  # median 0.45 / best 1.0
    assert report.metrics["share_profitable_trials"] == pytest.approx(0.6)
    assert report.metrics["n_trials"] == 5


def test_optional_robustness_gate(ds):
    test = _test(scores=[1.0, 0.1, 0.1, 0.1], min_robustness_ratio=0.5)
    report = test.run(SurfaceStrategy({"a": 10, "b": 0.5}), ds)
    assert not report.passed
    assert "robustness ratio" in report.notes


def test_without_a_run_it_falls_back_to_sharpe(ds):
    report = PlateauTest(max_workers=1).run(SurfaceStrategy({"a": 10, "b": 0.5}), ds)
    assert "objective=sharpe" in report.notes
