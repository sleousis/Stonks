"""Feature schema check and the training feature profile (roadmap 23.10)."""

from __future__ import annotations

import json

import numpy as np
import pytest

from stonks.features.feature_profile import (
    PROFILE_FILE,
    FeatureProfile,
    FeatureSchemaError,
    check_feature_schema,
)

NAMES = ("a", "b", "c")


def test_schema_check_passes_on_the_same_names_in_order():
    check_feature_schema(NAMES, ["a", "b", "c"])


@pytest.mark.parametrize(
    ("actual", "words"),
    [
        (["a", "b"], "missing"),
        (["a", "b", "c", "d"], "unexpected"),
        (["b", "a", "c"], "order"),
    ],
)
def test_schema_check_names_the_problem(actual, words):
    with pytest.raises(FeatureSchemaError, match=words):
        check_feature_schema(NAMES, actual)


def test_schema_error_is_a_value_error():
    assert issubclass(FeatureSchemaError, ValueError)


def _train(n: int = 500, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return np.column_stack([rng.normal(size=n), rng.uniform(size=n), rng.exponential(size=n)])


def test_profile_psi_is_small_on_the_same_distribution_and_large_after_a_shift():
    x = _train()
    profile = FeatureProfile.build(x, NAMES, bins=10)
    same = _train(seed=1)
    psi_same = profile.psi(same)
    assert set(psi_same) == set(NAMES)
    assert max(psi_same.values()) < 0.1
    shifted = same.copy()
    shifted[:, 0] += 2.0
    psi_shift = profile.psi(shifted)
    assert psi_shift["a"] > 0.5
    assert psi_shift["b"] < 0.1


def test_profile_ignores_non_finite_values_and_needs_some():
    x = _train()
    profile = FeatureProfile.build(x, NAMES)
    live = _train(seed=2)
    live[:10, 1] = np.nan
    assert np.isfinite(profile.psi(live)["b"])
    assert "b" not in profile.psi(np.full((5, 3), np.nan))


def test_profile_accepts_rows_as_mappings_in_any_order():
    profile = FeatureProfile.build(_train(), NAMES)
    rows = [{"c": 1.0, "a": 0.0, "b": 0.5}, {"b": 0.2, "c": 0.3, "a": -1.0}]
    assert set(profile.psi_rows(rows)) == set(NAMES)


def test_profile_round_trips_and_has_a_stable_id(tmp_path):
    profile = FeatureProfile.build(_train(), NAMES, bins=8)
    profile.save(tmp_path)
    assert (tmp_path / PROFILE_FILE).exists()
    again = FeatureProfile.load(tmp_path)
    assert again.names == NAMES
    assert again.profile_id == profile.profile_id
    assert json.loads((tmp_path / PROFILE_FILE).read_text())["names"] == list(NAMES)
    other = FeatureProfile.build(_train(seed=5), NAMES, bins=8)
    assert other.profile_id != profile.profile_id


def test_profile_rejects_a_matrix_of_the_wrong_width():
    profile = FeatureProfile.build(_train(), NAMES)
    with pytest.raises(FeatureSchemaError):
        profile.psi(np.zeros((4, 2)))
    with pytest.raises(FeatureSchemaError):
        FeatureProfile.build(np.zeros((4, 2)), NAMES)


def test_constant_feature_gets_one_bin_and_zero_psi_when_unchanged():
    x = _train()
    x[:, 2] = 1.0
    profile = FeatureProfile.build(x, NAMES)
    live = _train(seed=3)
    live[:, 2] = 1.0
    assert profile.psi(live)["c"] == pytest.approx(0.0, abs=1e-9)
