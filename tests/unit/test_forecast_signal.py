"""The ``forecast_signal`` example strategy (roadmap 23.11): it trades a
forecaster's view through the seam."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from stonks.features.forecasters.base import Forecaster
from stonks.lab.catalog import strategy_catalog
from stonks.strategies.examples.forecast_signal import ForecastSignal
from tests.fixtures.forecasting import ArForecaster, ar_lake

TICKERS = ["F00", "F01", "F02"]


@pytest.fixture(scope="module")
def lake():
    lake = ar_lake(":memory:", TICKERS, periods=300, phi=0.5)
    yield lake
    lake.close()


def _last_day(lake):
    return pd.Timestamp(lake.sql("SELECT MAX(date) AS d FROM prices").iloc[0]["d"]).date()


def test_catalogued_with_a_hypothesis_and_the_model_choices():
    assert strategy_catalog()["forecast_signal"] is ForecastSignal
    spec = {p.name: p for p in ForecastSignal.parameter_spec()}
    assert spec["model"].default == "theta"
    assert not spec["model"].tunable  # a model is a hypothesis, not a knob
    assert "chronos_bolt" in spec["model"].bounds
    assert ForecastSignal.hypothesis


def test_forecast_models_names_the_pinned_model():
    assert ForecastSignal.forecast_models({}) == ("theta",)
    assert ForecastSignal.forecast_models({"model": "chronos_2"}) == ("chronos_2",)


def test_forecaster_goes_through_the_registry():
    s = ForecastSignal({"model": "ets"})
    assert isinstance(s.forecaster(), Forecaster)
    assert s.forecaster().name == "ets"
    assert s.forecast_horizon == 5


def test_signal_is_the_forecast_log_return_and_long_only(lake, monkeypatch):
    s = ForecastSignal({"horizon": 1, "context_bars": 32})
    model = ArForecaster(phi=0.5)
    monkeypatch.setattr(s, "_model", model)
    day = _last_day(lake)
    values = {t: s.estimate_return(t, day, lake) for t in TICKERS}
    assert model.calls == len(TICKERS)
    feats = s.extract_features("F00", day, lake).values
    assert "forecast_return" in feats and "prob_up" in feats
    for t, v in values.items():
        r = s.extract_features(t, day, lake).values["forecast_return"]
        assert (v is None) == (r <= 0)
        if v is not None:
            assert v == pytest.approx(r)


def test_min_prob_up_filters_weak_views(lake, monkeypatch):
    s = ForecastSignal({"horizon": 1, "context_bars": 32, "min_prob_up": 0.95})
    monkeypatch.setattr(s, "_model", ArForecaster(phi=0.5))
    assert all(s.estimate_return(t, _last_day(lake), lake) is None for t in TICKERS)


def test_reads_no_bar_after_as_of(lake, monkeypatch):
    seen = []

    class Spy(ArForecaster):
        def predict_batch(self, contexts, horizon, levels=None):
            seen.extend(pd.to_datetime(c["timestamp"]).max() for c in contexts)
            return super().predict_batch(contexts, horizon)

    s = ForecastSignal({"horizon": 1, "context_bars": 32})
    monkeypatch.setattr(s, "_model", Spy())
    day = pd.bdate_range("2023-01-02", periods=300)[200].date()
    s.estimate_return("F01", day, lake)
    assert seen and max(seen).date() <= day


def test_short_history_gives_no_signal(lake):
    s = ForecastSignal({"context_bars": 64})
    early = pd.bdate_range("2023-01-02", periods=300)[10].date()
    assert s.estimate_return("F00", early, lake) is None
    r = s.estimate_return("F00", _last_day(lake), lake)
    assert r is None or math.isfinite(r)


def test_a_missing_optional_model_gives_no_signal_not_a_crash(lake):
    s = ForecastSignal({"model": "chronos_bolt"})
    if type(s.forecaster()).is_available():
        pytest.skip("chronos is installed")
    assert s.estimate_return("F00", _last_day(lake), lake) is None


def test_runs_end_to_end_with_the_baseline(lake):
    s = ForecastSignal({"horizon": 3, "context_bars": 48})
    out = [s.estimate_return(t, _last_day(lake), lake) for t in TICKERS]
    assert all(v is None or (math.isfinite(v) and v > 0) for v in out)
    assert np.isfinite(
        [s.extract_features(t, _last_day(lake), lake).values["forecast_return"] for t in TICKERS]
    ).all()
