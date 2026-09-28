"""Regressor seam over scikit-learn's histogram gradient boosting (roadmap 23.12)."""

from __future__ import annotations

import json

import numpy as np
import pytest

from stonks.features.ml import (
    GradientBoostingRegressor,
    Regressor,
    make_regressor,
    regressor_kinds,
)


def _linear(n=400, seed=0):
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(n, 3))
    y = x[:, 0] + 0.1 * rng.normal(size=n)
    return x, y


def test_gbm_learns_a_monotone_signal():
    x, y = _linear()
    model = GradientBoostingRegressor(max_iter=100, seed=1)
    assert isinstance(model, Regressor)
    model.fit(x, y)
    pred = model.predict(np.array([[2.0, 0.0, 0.0], [-2.0, 0.0, 0.0]]))
    assert pred.shape == (2,)
    assert pred[0] > 1.0 and pred[1] < -1.0


def test_gbm_is_deterministic_for_a_seed():
    x, y = _linear()
    a = GradientBoostingRegressor(max_iter=50, seed=3)
    b = GradientBoostingRegressor(max_iter=50, seed=3)
    a.fit(x, y)
    b.fit(x, y)
    np.testing.assert_array_equal(a.predict(x), b.predict(x))


def test_gbm_accepts_missing_values():
    x, y = _linear()
    x[::7, 1] = np.nan
    model = GradientBoostingRegressor(max_iter=30)
    model.fit(x, y)
    assert np.isfinite(model.predict(np.array([[np.nan, np.nan, np.nan]]))).all()


def test_gbm_rejects_bad_input_and_unfitted_use():
    model = GradientBoostingRegressor()
    with pytest.raises(RuntimeError):
        model.predict(np.zeros((1, 3)))
    with pytest.raises(ValueError):
        model.fit(np.zeros((0, 3)), np.zeros(0))
    with pytest.raises(ValueError):
        model.fit(np.zeros((3, 2)), np.zeros(2))
    with pytest.raises(ValueError):
        GradientBoostingRegressor(learning_rate=0.0)


def test_gbm_constant_target_predicts_the_constant():
    model = GradientBoostingRegressor(max_iter=10)
    model.fit(np.random.default_rng(0).normal(size=(20, 2)), np.full(20, 0.25))
    np.testing.assert_allclose(model.predict(np.zeros((2, 2))), 0.25)


def test_gbm_round_trips_through_save_and_load(tmp_path):
    x, y = _linear()
    model = GradientBoostingRegressor(max_iter=40, max_leaf_nodes=7, seed=2)
    model.fit(x, y)
    model.save(tmp_path / "m")
    loaded = GradientBoostingRegressor.load(tmp_path / "m")
    np.testing.assert_array_equal(loaded.predict(x), model.predict(x))
    assert loaded.max_leaf_nodes == 7


def test_gbm_load_refuses_a_tampered_file(tmp_path):
    x, y = _linear()
    model = GradientBoostingRegressor(max_iter=10)
    model.fit(x, y)
    model.save(tmp_path / "m")
    blob = tmp_path / "m" / "model.joblib"
    blob.write_bytes(blob.read_bytes() + b"x")
    with pytest.raises(ValueError, match="digest"):
        GradientBoostingRegressor.load(tmp_path / "m")


def test_gbm_load_refuses_another_kind(tmp_path):
    x, y = _linear()
    model = GradientBoostingRegressor(max_iter=10)
    model.fit(x, y)
    model.save(tmp_path / "m")
    meta = tmp_path / "m" / "model.json"
    data = json.loads(meta.read_text())
    data["kind"] = "random_forest"
    meta.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="not a"):
        GradientBoostingRegressor.load(tmp_path / "m")


def test_regressor_registry_builds_by_kind():
    assert "hist_gbm" in regressor_kinds()
    model = make_regressor("hist_gbm", max_iter=5, seed=4)
    assert isinstance(model, GradientBoostingRegressor) and model.seed == 4
    with pytest.raises(ValueError, match="unknown regressor"):
        make_regressor("nope")
