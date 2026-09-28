"""The ``forecast_skill`` survival test (roadmap 23.11): a forecaster must
beat the random walk and the better of ETS and Theta."""

from __future__ import annotations

import pytest

from stonks.assistant.settings import CUTOFF_SAFE_TESTS
from stonks.features.forecasters.base import DEFAULT_LEVELS
from stonks.lab.survival.registry import SUITE_PRESETS, build_survival_test, survival_test_names
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from tests.fixtures.forecasting import (
    ArForecaster,
    ForecastUser,
    NoiseForecaster,
    PretrainedAr,
    ar_lake,
)
from tests.fixtures.signal_research import dataset_for

TICKERS = [f"F{i:02d}" for i in range(6)]


@pytest.fixture(scope="module")
def lake():
    lake = ar_lake(":memory:", TICKERS, periods=400)
    yield lake
    lake.close()


def _test(**options):
    base = {"horizon": 1, "every_bars": 1}
    return build_survival_test("forecast_skill", {**base, **options})


def test_registered_and_cutoff_safe_but_in_no_preset():
    # no preset lists it, so strategies registered before it keep their
    # go-live checklist; the runner adds it for strategies that forecast
    assert "forecast_skill" in survival_test_names()
    assert all("forecast_skill" not in ids for ids in SUITE_PRESETS.values())
    assert "forecast_skill" in CUTOFF_SAFE_TESTS


def test_strategies_without_a_forecaster_pass_as_na(lake):
    report = _test().run(BuyAndHold({}), dataset_for(lake, TICKERS))
    assert report.passed
    assert report.notes.startswith("n/a")
    report = _test().run(ForecastUser(None), dataset_for(lake, TICKERS))
    assert report.passed


def test_a_skilled_model_beats_every_baseline(lake):
    ds = dataset_for(lake, TICKERS)
    model = ArForecaster(phi=0.5)
    report = _test().run(ForecastUser(model), ds)
    m = report.metrics
    assert report.passed, report.notes
    assert m["dm_p_random_walk"] < 0.01
    assert m["dm_p_stat_baseline"] < 0.01
    assert m["mase_model"] < m["mase_random_walk"]
    assert m["crps_model"] < m["crps_random_walk"]
    assert m["rank_ic"] > 0.2
    assert m["directional_accuracy"] > 0.55
    assert "(the better of ets/theta)" in report.notes
    # only validation-window origins were scored
    assert m["n_dates"] <= 200
    assert m["n_forecasts"] == model.calls
    assert str(ds.val_window[0]) in report.notes


def test_a_model_with_no_skill_fails(lake):
    report = _test().run(ForecastUser(NoiseForecaster()), dataset_for(lake, TICKERS))
    assert not report.passed
    assert report.metrics["dm_p_random_walk"] > 0.05


def test_the_random_walk_cannot_beat_itself(lake):
    from stonks.features.forecasters import build_forecaster

    report = _test().run(ForecastUser(build_forecaster("random_walk")), dataset_for(lake, TICKERS))
    assert not report.passed
    assert report.metrics["dm_p_random_walk"] == 1.0


def test_a_window_inside_the_pretraining_data_fails(lake):
    # the lake starts 2023-01-02; the validation window sits in 2024 but the
    # cutoff is moved past it
    ds = dataset_for(lake, TICKERS)

    class Late(PretrainedAr):
        release_date = ds.val_window[0]

    report = _test().run(ForecastUser(Late()), ds)
    assert not report.passed
    assert "cutoff" in report.notes
    ok = _test().run(ForecastUser(PretrainedAr()), ds)
    assert ok.passed, ok.notes


def test_too_few_forecasts_fail_closed(lake):
    report = _test(every_bars=50).run(ForecastUser(ArForecaster()), dataset_for(lake, TICKERS))
    assert not report.passed
    assert "too few" in report.notes


def test_a_rank_ic_floor_can_be_required(lake):
    report = _test(min_rank_ic=0.99).run(ForecastUser(ArForecaster()), dataset_for(lake, TICKERS))
    assert not report.passed


def test_longer_horizons_use_hac_lags(lake):
    report = _test(horizon=5, every_bars=1).run(
        ForecastUser(ArForecaster()), dataset_for(lake, TICKERS)
    )
    assert report.metrics["dm_lags"] >= 4


def test_rejects_bad_options():
    with pytest.raises(ValueError):
        build_survival_test("forecast_skill", {"alpha": 0.9})
    with pytest.raises(ValueError):
        build_survival_test("forecast_skill", {"stat_baselines": ["random_walk"]})
    with pytest.raises(ValueError):
        build_survival_test("forecast_skill", {"stat_baselines": ["nope"]})


def test_the_runner_adds_the_test_for_strategies_that_forecast():
    from stonks.lab.runner import LabRunner
    from stonks.lab.survival.base import SurvivalSuite

    oos = build_survival_test("oos")
    runner = LabRunner(tuner=None, objective=None, suite=SurvivalSuite([oos]), budget=1)  # type: ignore[arg-type]
    ids = [t.id for t in runner._suite_for(ForecastUser(ArForecaster())).tests]
    assert ids == ["oos", "forecast_skill"]
    assert [t.id for t in runner._suite_for(BuyAndHold({})).tests] == ["oos"]
    both = LabRunner(
        tuner=None,  # type: ignore[arg-type]
        objective=None,  # type: ignore[arg-type]
        suite=SurvivalSuite([oos, _test()]),
        budget=1,
    )
    assert [t.id for t in both._suite_for(ForecastUser(ArForecaster())).tests] == [
        "oos",
        "forecast_skill",
    ]


def test_a_baseline_with_a_nan_forecast_is_never_the_better_one(lake, monkeypatch):
    """A statistical baseline that returns NaN on one origin has a NaN MSE.
    It must not be picked as the better baseline (``min`` keeps a NaN
    first key), or the model is judged against the weaker one."""
    import math

    from stonks.features.forecasters.base import Forecast
    from stonks.lab.survival import forecast_skill as mod

    class NanNoise(NoiseForecaster):
        def predict_batch(self, contexts, horizon, levels=DEFAULT_LEVELS):
            out = super().predict_batch(contexts, horizon, levels)
            first = out[0]
            out[0] = Forecast(math.nan, first.levels, first.quantiles)
            return out

    real = mod.build_forecaster

    def fake(name, *args, **kwargs):
        if name == "ets":
            return NanNoise()
        if name == "theta":
            return ArForecaster(phi=0.5)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(mod, "build_forecaster", fake)
    report = _test().run(ForecastUser(ArForecaster(phi=0.5)), dataset_for(lake, TICKERS))
    assert "theta (the better of" in report.notes
    assert not report.passed  # the model is the theta baseline: no skill over it
