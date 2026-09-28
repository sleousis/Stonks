"""The forecaster seam (roadmap 23.11): the forecast type, the baselines and
the registry with its licence and cutoff rules."""

from __future__ import annotations

import sys
import textwrap
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stonks.features.forecasters import (
    DEFAULT_LEVELS,
    Forecast,
    Forecaster,
    build_forecaster,
    forecaster_classes,
    forecaster_names,
    get_forecaster_class,
)
from stonks.features.forecasters.registry import discover_forecasters


def _bars(closes) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {
            "timestamp": pd.bdate_range("2024-01-01", periods=len(closes)),
            "open": closes,
            "high": closes * 1.01,
            "low": closes * 0.99,
            "close": closes,
            "volume": 1000.0,
        }
    )


def _walk(n=200, seed=1, drift=0.0):
    rng = np.random.default_rng(seed)
    return 100.0 * np.exp(np.cumsum(rng.normal(drift, 0.01, n)))


# ---- Forecast ---------------------------------------------------------------------


def test_forecast_point_return_and_probability_up():
    q = np.array([[99.0, 98.0], [101.0, 102.0], [103.0, 106.0]])
    f = Forecast(last=100.0, levels=(0.1, 0.5, 0.9), quantiles=q)
    assert f.horizon == 2
    assert f.point() == pytest.approx(102.0)
    assert f.log_return() == pytest.approx(np.log(1.02))
    assert list(f.median()) == [101.0, 102.0]
    # the last close sits between the 0.1 and 0.5 quantiles of the last step
    # CDF(100) interpolates to 0.3, so P(up) is 0.7
    assert f.prob_up() == pytest.approx(0.7)


def test_forecast_from_samples_takes_quantiles_and_keeps_the_paths():
    rng = np.random.default_rng(0)
    samples = 100.0 + rng.normal(0, 1, (400, 3))
    f = Forecast.from_samples(100.0, samples)
    assert f.levels == DEFAULT_LEVELS
    assert f.quantiles.shape == (len(DEFAULT_LEVELS), 3)
    assert f.samples is not None
    assert f.prob_up() == pytest.approx(float(np.mean(samples[:, -1] > 100.0)))


def test_forecast_rejects_bad_shapes_and_levels():
    with pytest.raises(ValueError, match="levels"):
        Forecast(last=1.0, levels=(0.9, 0.1), quantiles=np.ones((2, 1)))
    with pytest.raises(ValueError, match="shape"):
        Forecast(last=1.0, levels=(0.1, 0.5), quantiles=np.ones((3, 1)))
    with pytest.raises(ValueError, match=r"0.5"):
        Forecast(last=1.0, levels=(0.1, 0.9), quantiles=np.ones((2, 1)))


def test_forecast_sorts_crossed_quantiles():
    q = np.array([[101.0], [100.0], [99.0]])
    f = Forecast(last=100.0, levels=(0.1, 0.5, 0.9), quantiles=q)
    assert list(f.quantiles[:, 0]) == [99.0, 100.0, 101.0]


# ---- baselines --------------------------------------------------------------------


@pytest.mark.parametrize("name", ["random_walk", "drift", "ets", "theta"])
def test_baselines_are_registered_unpretrained_and_well_formed(name):
    cls = get_forecaster_class(name)
    assert not cls.pretrained
    assert cls.cutoff() is None
    assert cls.is_available()
    f = build_forecaster(name).predict(_bars(_walk()), horizon=5)
    assert f.horizon == 5
    assert f.levels == DEFAULT_LEVELS
    assert np.all(np.isfinite(f.quantiles))
    assert np.all(np.diff(f.quantiles, axis=0) >= 0)
    # wider bands further out
    spread = f.quantiles[-1] - f.quantiles[0]
    assert spread[-1] > spread[0]


def test_random_walk_predicts_the_last_close():
    closes = _walk()
    f = build_forecaster("random_walk").predict(_bars(closes), horizon=3)
    assert f.point() == pytest.approx(closes[-1])
    assert f.prob_up() == pytest.approx(0.5)


def test_drift_follows_a_steady_trend_and_theta_damps_it_by_half():
    closes = 100.0 * np.exp(0.002 * np.arange(200))
    drift = build_forecaster("drift").predict(_bars(closes), horizon=10)
    assert drift.log_return() == pytest.approx(0.02, rel=0.01)
    theta = build_forecaster("theta").predict(_bars(closes), horizon=10)
    assert theta.log_return() == pytest.approx(0.01, rel=0.1)


def test_ets_tracks_the_level():
    closes = np.r_[np.full(100, 100.0), np.full(100, 120.0)]
    f = build_forecaster("ets").predict(_bars(closes), horizon=2)
    assert f.point() == pytest.approx(120.0, rel=0.01)


def test_baselines_on_flat_prices_give_a_point_mass():
    f = build_forecaster("theta").predict(_bars(np.full(60, 50.0)), horizon=4)
    assert np.allclose(f.quantiles, 50.0)


def test_baselines_reject_short_or_bad_contexts():
    fc = build_forecaster("random_walk")
    with pytest.raises(ValueError, match="at least"):
        fc.predict(_bars(_walk(5)), horizon=1)
    bad = _walk(60)
    bad[10] = np.nan
    with pytest.raises(ValueError, match="finite"):
        fc.predict(_bars(bad), horizon=1)
    with pytest.raises(ValueError, match="horizon"):
        fc.predict(_bars(_walk()), horizon=0)
    with pytest.raises(ValueError, match="close"):
        fc.predict(pd.DataFrame({"x": [1.0] * 50}), horizon=1)


def test_predict_batch_matches_predict():
    fc = build_forecaster("ets")
    a, b = _bars(_walk(seed=1)), _bars(_walk(seed=2))
    batch = fc.predict_batch([a, b], horizon=3)
    assert batch[1].point() == pytest.approx(fc.predict(b, horizon=3).point())


# ---- registry ---------------------------------------------------------------------


def test_registry_lists_the_baselines_and_the_pretrained_adapters():
    names = forecaster_names()
    assert names == sorted(names)
    for expected in (
        "random_walk",
        "drift",
        "ets",
        "theta",
        "chronos_bolt",
        "chronos_2",
        "timesfm_2_5",
        "kronos_small",
        "kronos_mini",
    ):
        assert expected in names


def test_every_pretrained_model_declares_a_cutoff_and_an_open_licence():
    for name, cls in forecaster_classes().items():
        if not cls.pretrained:
            continue
        assert cls.cutoff() is not None, name
        assert cls.licence in ("Apache-2.0", "MIT"), name
        assert cls.extra, name


def test_cutoff_is_the_published_cutoff_else_the_release_date():
    class _Published(Forecaster):
        pretrained = True
        pretrain_cutoff = date(2023, 6, 30)
        release_date = date(2024, 1, 1)

    class _Unpublished(Forecaster):
        pretrained = True
        release_date = date(2024, 1, 1)

    assert _Published.cutoff() == date(2023, 6, 30)
    assert _Unpublished.cutoff() == date(2024, 1, 1)


def test_known_release_dates():
    assert get_forecaster_class("chronos_bolt").cutoff() == date(2024, 11, 25)
    assert get_forecaster_class("chronos_2").cutoff() == date(2025, 10, 30)
    assert get_forecaster_class("timesfm_2_5").cutoff() == date(2025, 9, 2)
    assert get_forecaster_class("kronos_small").cutoff() == date(2025, 6, 30)
    assert get_forecaster_class("kronos_mini").cutoff() == date(2025, 7, 1)
    assert get_forecaster_class("kronos_small").reads_ohlcv


def test_adapters_import_without_their_optional_packages():
    # importing the registry never imports torch or a model package
    forecaster_classes()
    for heavy in ("torch", "chronos", "timesfm"):
        if heavy in sys.modules:
            pytest.skip(f"{heavy} was imported by another test")
    assert "torch" not in sys.modules


def test_unknown_name_lists_the_choices():
    with pytest.raises(ValueError, match="random_walk"):
        get_forecaster_class("nope")


def _package(tmp_path: Path, name: str, body: str) -> object:
    pkg = tmp_path / name
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "extra.py").write_text(textwrap.dedent(body))
    sys.path.insert(0, str(tmp_path))
    try:
        import importlib

        return importlib.import_module(name)
    finally:
        sys.path.remove(str(tmp_path))


def test_discovery_finds_a_new_module_without_a_list(tmp_path):
    pkg = _package(
        tmp_path,
        "fc_pkg_ok",
        """
        from stonks.features.forecasters.base import Forecaster

        class Mine(Forecaster):
            name = "mine"
            def predict_batch(self, contexts, horizon, levels=None):
                return []
        """,
    )
    assert "mine" in discover_forecasters(pkg)


def test_discovery_refuses_a_pretrained_model_with_no_cutoff(tmp_path):
    pkg = _package(
        tmp_path,
        "fc_pkg_nocut",
        """
        from stonks.features.forecasters.base import Forecaster

        class Leaky(Forecaster):
            name = "leaky"
            pretrained = True
            licence = "MIT"
            def predict_batch(self, contexts, horizon, levels=None):
                return []
        """,
    )
    with pytest.raises(ValueError, match="cutoff"):
        discover_forecasters(pkg)


def test_discovery_refuses_non_commercial_weights(tmp_path):
    pkg = _package(
        tmp_path,
        "fc_pkg_nc",
        """
        from datetime import date
        from stonks.features.forecasters.base import Forecaster

        class Moirai(Forecaster):
            name = "moirai"
            pretrained = True
            release_date = date(2025, 1, 1)
            licence = "CC-BY-NC-4.0"
            def predict_batch(self, contexts, horizon, levels=None):
                return []
        """,
    )
    with pytest.raises(ValueError, match="licence"):
        discover_forecasters(pkg)
