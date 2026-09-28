"""The pretrained forecaster adapters (roadmap 23.11).

Two kinds of test. Hermetic ones swap in fake model packages to check each
adapter's glue (shapes, levels, the install hint, the TimesFM 3.0 refusal).
Real ones run a model only when its optional group is installed and its
weights are already cached: they pass ``local_files_only=True`` and skip
otherwise, so no test ever downloads weights.
"""

from __future__ import annotations

import sys
import textwrap
import types

import numpy as np
import pandas as pd
import pytest

from stonks.features.forecasters import build_forecaster, forecaster_classes
from stonks.features.forecasters import kronos as kronos_mod
from stonks.features.forecasters._pretrained import (
    ModelUnavailableError,
    PretrainedForecaster,
    interpolate_levels,
)

LEVELS = (0.1, 0.5, 0.9)


def _bars(n=128, seed=3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    closes = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    return pd.DataFrame(
        {
            "timestamp": pd.bdate_range("2024-01-01", periods=n),
            "open": closes,
            "high": closes * 1.01,
            "low": closes * 0.99,
            "close": closes,
            "volume": 1000.0,
        }
    )


PRETRAINED = sorted(n for n, c in forecaster_classes().items() if c.pretrained)


def test_interpolate_levels_between_and_outside_the_source():
    src = (0.1, 0.5, 0.9)
    values = np.array([[1.0], [2.0], [3.0]])
    out = interpolate_levels(src, values, (0.05, 0.3, 0.5, 0.95))
    assert out[:, 0].tolist() == pytest.approx([1.0, 1.5, 2.0, 3.0])


@pytest.mark.parametrize("name", PRETRAINED)
def test_missing_packages_raise_an_install_hint(name, monkeypatch):
    cls = forecaster_classes()[name]
    monkeypatch.setattr(cls, "missing", classmethod(lambda c: ["torch"]))
    model = build_forecaster(name)
    assert isinstance(model, PretrainedForecaster)
    with pytest.raises(ModelUnavailableError, match=f"uv sync --extra {cls.extra}"):
        model.predict(_bars(), horizon=2)


def test_timesfm_refuses_the_non_commercial_3_0_weights():
    with pytest.raises(ValueError, match="non-commercial"):
        build_forecaster("timesfm_2_5", model_id="google/timesfm-3.0-500m")


def test_max_context_below_the_minimum_is_refused():
    with pytest.raises(ValueError, match="max_context"):
        build_forecaster("chronos_bolt", max_context=4)


# ---- hermetic glue with fake packages ------------------------------------------------


class _FakeTensor(np.ndarray):
    def detach(self):
        return self

    def cpu(self):
        return self

    def numpy(self):
        return np.asarray(self)


def _fake_torch():
    torch = types.ModuleType("torch")
    torch.float32 = "float32"  # type: ignore[attr-defined]
    torch.tensor = lambda data, dtype=None: np.asarray(data, dtype=float)  # type: ignore[attr-defined]
    return torch


def test_chronos_glue_maps_the_pipeline_output(monkeypatch):
    calls = {}

    class Pipeline:
        @classmethod
        def from_pretrained(cls, model_id, **kw):
            calls["load"] = (model_id, kw)
            return cls()

        def predict_quantiles(self, inputs, prediction_length, quantile_levels):
            calls["n"] = len(inputs)
            out = []
            for series in inputs:
                last = float(series[-1])
                grid = np.array([[last * (1 + (lv - 0.5) * 0.1) for lv in quantile_levels]])
                out.append(np.repeat(grid, prediction_length, axis=0).view(_FakeTensor))
            return out, None

    chronos = types.ModuleType("chronos")
    chronos.BaseChronosPipeline = Pipeline  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "chronos", chronos)
    monkeypatch.setitem(sys.modules, "torch", _fake_torch())
    cls = forecaster_classes()["chronos_bolt"]
    monkeypatch.setattr(cls, "missing", classmethod(lambda c: []))
    model = build_forecaster("chronos_bolt", local_files_only=True)
    a, b = _bars(seed=1), _bars(seed=2)
    out = model.predict_batch([a, b], horizon=3, levels=LEVELS)
    assert calls["load"] == (
        "amazon/chronos-bolt-small",
        {"device_map": "cpu", "local_files_only": True},
    )
    assert calls["n"] == 2
    assert out[1].last == pytest.approx(b["close"].iloc[-1])
    assert out[1].point() == pytest.approx(b["close"].iloc[-1])
    assert out[0].quantiles.shape == (3, 3)


def test_timesfm_glue_reads_the_deciles(monkeypatch):
    class Model:
        @classmethod
        def from_pretrained(cls, model_id, **kw):
            return cls()

        def compile(self, config):
            self.config = config

        def forecast(self, horizon, inputs):
            q = np.zeros((len(inputs), horizon, 10))
            for i, series in enumerate(inputs):
                q[i, :, 0] = -1.0  # the mean column is ignored
                for j, lv in enumerate(np.arange(1, 10) / 10):
                    q[i, :, j + 1] = series[-1] + (lv - 0.5)
            return q[:, :, 0], q

    timesfm = types.ModuleType("timesfm")
    timesfm.TimesFM_2p5_200M_torch = Model  # type: ignore[attr-defined]
    timesfm.ForecastConfig = lambda **kw: kw  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "timesfm", timesfm)
    cls = forecaster_classes()["timesfm_2_5"]
    monkeypatch.setattr(cls, "missing", classmethod(lambda c: []))
    ctx = _bars()
    f = build_forecaster("timesfm_2_5").predict(ctx, horizon=4, levels=(0.05, 0.5, 0.9))
    last = ctx["close"].iloc[-1]
    assert f.point() == pytest.approx(last)
    assert f.quantiles[0, 0] == pytest.approx(last - 0.4)  # clamped to the 0.1 decile
    assert f.quantiles[2, 0] == pytest.approx(last + 0.4)


def test_kronos_loads_the_clone_and_samples_paths(tmp_path, monkeypatch):
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "__init__.py").write_text(
        textwrap.dedent(
            """
            from .kronos import Kronos, KronosPredictor, KronosTokenizer
            """
        )
    )
    (model_dir / "kronos.py").write_text(
        textwrap.dedent(
            """
            import itertools
            import pandas as pd

            class KronosTokenizer:
                @classmethod
                def from_pretrained(cls, name, **kw):
                    return cls()

            class Kronos:
                @classmethod
                def from_pretrained(cls, name, **kw):
                    return cls()

            class KronosPredictor:
                def __init__(self, model, tokenizer, device=None, max_context=512):
                    self.bumps = itertools.cycle([-1.0, 1.0])

                def predict(self, df, x_timestamp, y_timestamp, pred_len, **kw):
                    last = float(df["close"].iloc[-1])
                    bump = next(self.bumps)
                    return pd.DataFrame(
                        {"close": [last + bump] * pred_len}, index=y_timestamp
                    )
            """
        )
    )
    monkeypatch.setenv(kronos_mod.REPO_ENV, str(tmp_path))
    monkeypatch.delitem(sys.modules, kronos_mod._MODULE, raising=False)
    cls = forecaster_classes()["kronos_small"]
    monkeypatch.setattr(cls, "missing", classmethod(lambda c: []))
    ctx = _bars()
    f = build_forecaster("kronos_small", n_samples=4).predict(ctx, horizon=2)
    assert f.samples is not None
    assert f.samples.shape == (4, 2)
    assert f.prob_up() == pytest.approx(0.5)
    with pytest.raises(ValueError, match="OHLCV"):
        build_forecaster("kronos_small", n_samples=1).predict(ctx[["timestamp", "close"]], 1)
    monkeypatch.delitem(sys.modules, kronos_mod._MODULE, raising=False)


def test_kronos_without_a_clone_says_how_to_get_it(monkeypatch):
    monkeypatch.delenv(kronos_mod.REPO_ENV, raising=False)
    cls = forecaster_classes()["kronos_mini"]
    monkeypatch.setattr(cls, "missing", classmethod(lambda c: []))
    assert not cls.is_available() or kronos_mod._repo(None) is not None
    with pytest.raises(ModelUnavailableError, match=kronos_mod.REPO_ENV):
        build_forecaster("kronos_mini").predict(_bars(), horizon=1)


# ---- real models: only with the optional group and cached weights -----------------------


@pytest.mark.slow
@pytest.mark.parametrize("name", PRETRAINED)
def test_real_model_forecasts_when_installed_and_cached(name):
    cls = forecaster_classes()[name]
    if not cls.is_available():
        pytest.skip(f"{name}: optional group {cls.extra!r} is not installed")
    model = build_forecaster(name, local_files_only=True)
    try:
        model.model()
    except Exception as exc:  # weights not in the local cache: never download
        pytest.skip(f"{name}: weights are not cached locally ({type(exc).__name__})")
    f = model.predict(_bars(), horizon=3)
    assert f.horizon == 3
    assert np.all(np.isfinite(f.quantiles))
    assert f.quantiles[0, -1] <= f.point() <= f.quantiles[-1, -1]
