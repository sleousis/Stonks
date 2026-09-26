"""VolForecaster seam: EWMA, GARCH(1,1)-t (arch, wrapped) and HAR-RV (BL-48)."""

from __future__ import annotations

import json
import math

import numpy as np
import pytest

from stonks.features.vol_forecast import (
    VOL_FORECASTERS,
    EwmaVol,
    GarchVol,
    HarRv,
    build_vol_forecaster,
)


def _garch_path(n=3000, omega=2e-6, alpha=0.08, beta=0.9, seed=0):
    rng = np.random.default_rng(seed)
    z = rng.standard_t(6, n) / math.sqrt(6 / 4)  # unit variance
    r = np.empty(n)
    v = omega / (1 - alpha - beta)
    for t in range(n):
        r[t] = math.sqrt(v) * z[t]
        v = omega + alpha * r[t] ** 2 + beta * v
    return r


def test_registry():
    assert set(VOL_FORECASTERS) == {"ewma", "garch", "har_rv"}
    assert isinstance(build_vol_forecaster("ewma", lam=0.9), EwmaVol)
    with pytest.raises(ValueError):
        build_vol_forecaster("nope")


def test_ewma_of_a_constant_size_series_is_that_size():
    r = np.array([0.01, -0.01] * 100)
    f = EwmaVol().fit(r)
    assert f.forecast() == pytest.approx(0.01)
    assert f.forecast(10) == pytest.approx(0.01)
    assert f.conditional_vol()[-1] == pytest.approx(0.01)


def test_ewma_follows_the_riskmetrics_recursion():
    r = np.random.default_rng(1).normal(0, 0.01, 300)
    vol = EwmaVol(lam=0.94).fit(r).conditional_vol()
    assert vol[150] ** 2 == pytest.approx(0.94 * vol[149] ** 2 + 0.06 * r[149] ** 2)


def test_garch_recovers_a_simulated_process_and_leaks_no_library_types():
    r = _garch_path()
    f = GarchVol().fit(r)
    assert f.alpha + f.beta == pytest.approx(0.98, abs=0.03)
    assert f.nu is not None and 3 < f.nu < 15
    data = f.to_dict()
    json.dumps(data)  # plain floats only
    assert all(isinstance(v, float | str | None) for v in data.values())
    assert f.conditional_vol().shape == r.shape
    assert f.standardized_residuals().std() == pytest.approx(1.0, abs=0.1)


def test_garch_long_forecast_tends_to_the_unconditional_variance():
    f = GarchVol(dist="normal").fit(_garch_path(seed=2))
    long_run = f.forecast(5000)
    assert long_run == pytest.approx(math.sqrt(f.unconditional_variance()), rel=0.05)
    assert f.forecast(1) == pytest.approx(math.sqrt(f.next_variance()))


def test_garch_simulation_follows_the_recursion():
    f = GarchVol().fit(_garch_path(seed=3))
    zero = f.simulate(np.zeros(5))
    np.testing.assert_allclose(zero, f.mu)
    shocked = f.simulate(np.array([3.0, 0.0]))
    assert shocked[0] == pytest.approx(f.mu + 3.0 * math.sqrt(f.next_variance()))


def test_har_on_constant_squared_returns():
    r = np.array([0.02, -0.02] * 60)
    f = HarRv().fit(r)
    assert f.forecast() == pytest.approx(0.02, rel=1e-6)
    assert f.forecast(5) == pytest.approx(0.02, rel=1e-6)
    assert f.conditional_vol().shape == r.shape


def test_har_tracks_a_volatility_jump():
    rng = np.random.default_rng(4)
    r = np.concatenate([rng.normal(0, 0.005, 300), rng.normal(0, 0.03, 60)])
    f = HarRv().fit(r)
    assert f.forecast() > 0.01


def test_unfitted_and_bad_input():
    for cls in (EwmaVol, GarchVol, HarRv):
        with pytest.raises(RuntimeError):
            cls().forecast()
        with pytest.raises(RuntimeError):
            cls().conditional_vol()
    with pytest.raises(ValueError):
        GarchVol().fit(np.zeros(5))
    with pytest.raises(ValueError):
        EwmaVol(lam=1.0)
    with pytest.raises(ValueError):
        GarchVol(dist="skewt")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        EwmaVol().fit(np.array([0.01, 0.02, 0.0])).forecast(0)
    with pytest.raises(RuntimeError):
        GarchVol().simulate(np.zeros(2))
