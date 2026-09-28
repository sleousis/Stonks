"""The pretraining cutoff rule (roadmap 23.11): the lab refuses a
validation window that starts on or before a model's cutoff."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.features.forecasters.cutoff import cutoff_violations, strategy_forecast_models
from stonks.lab.dataset import LabDataset
from stonks.lab.preflight import PreflightError, forecast_cutoff_issues, run_preflight
from stonks.lab.runner import LabRunner
from stonks.lab.survival.base import SurvivalSuite
from stonks.strategies.examples.buy_and_hold import BuyAndHold

# chronos_bolt's cutoff is its release date, 2024-11-25


class _Uses:
    """A strategy class that runs the pinned model, else ``chronos_bolt``."""

    @classmethod
    def forecast_models(cls, params):
        return [params.get("model", "chronos_bolt")]


def _ds(start, end, lake=None, train_ratio=0.5):
    return LabDataset(lake=lake, universe=["A.US"], start=start, end=end, train_ratio=train_ratio)


def test_strategies_without_the_hook_use_no_model():
    assert strategy_forecast_models(BuyAndHold) == ()
    assert strategy_forecast_models(_Uses) == ("chronos_bolt",)
    assert strategy_forecast_models(_Uses, {"model": "theta"}) == ("theta",)


def test_violations_on_or_before_the_cutoff_only():
    assert [v.model for v in cutoff_violations(["chronos_bolt"], date(2024, 11, 25))] == [
        "chronos_bolt"
    ]
    assert cutoff_violations(["chronos_bolt"], date(2024, 11, 26)) == []
    assert cutoff_violations(["theta", "random_walk"], date(2000, 1, 1)) == []


def test_unknown_models_fail_closed():
    (v,) = cutoff_violations(["no_such_model"], date(2030, 1, 1))
    assert v.cutoff is None


def test_preflight_refuses_a_window_inside_the_pretraining_data():
    # validation starts 2024-07-01, before the 2024-11-25 cutoff
    report = run_preflight(_ds(date(2024, 1, 1), date(2024, 12, 31)), _Uses)
    codes = [i.code for i in report.issues if i.severity == "error"]
    assert codes == ["forecast_cutoff"]
    assert "2024-11-25" in report.errors[0].message
    assert report.skipped  # no lake: the data checks were skipped, the rule was not
    with pytest.raises(PreflightError, match="forecast_cutoff"):
        report.raise_for_errors()


def test_preflight_accepts_a_window_after_the_cutoff_and_baselines_anywhere():
    after = _ds(date(2024, 6, 1), date(2025, 12, 31))
    assert forecast_cutoff_issues(after, _Uses) == []
    before = _ds(date(2010, 1, 1), date(2012, 1, 1))
    assert forecast_cutoff_issues(before, _Uses, {"model": "theta"}) == []


def test_pinned_params_are_checked():
    ds = _ds(date(2025, 1, 1), date(2026, 6, 30))  # validation starts 2025-10-01
    issues = forecast_cutoff_issues(ds, _Uses, {"model": "chronos_2"})  # cutoff 2025-10-30
    assert [i.details["model"] for i in issues] == ["chronos_2"]


class _Tuner:
    seed = 1
    calls = 0

    def tune(self, *a, **k):  # pragma: no cover - never reached
        self.calls += 1
        raise AssertionError("tuned a refused run")


class _Objective:
    name = "fake"
    direction = "maximize"

    def score(self, strategy, dataset):
        return 0.0


class _ForecastBuyAndHold(BuyAndHold):
    @classmethod
    def forecast_models(cls, params):
        return ["timesfm_2_5"]


@pytest.mark.parametrize("preflight", [True, False])
def test_the_runner_refuses_before_tuning_even_with_the_preflight_off(preflight):
    runner = LabRunner(
        tuner=_Tuner(),
        objective=_Objective(),
        suite=SurvivalSuite([]),
        budget=1,
        preflight=preflight,
    )
    ds = _ds(date(2024, 1, 1), date(2025, 6, 30))
    with pytest.raises(PreflightError, match="forecast_cutoff"):
        runner.run(_ForecastBuyAndHold, ds)
