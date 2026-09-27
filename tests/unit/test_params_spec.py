"""Unit tests for ParameterSpec / ParamSpace — the declarative parameter surface
that strategies expose and tuners consume. Must be fully agnostic of strategies."""

import dataclasses

import pytest

from stonks.core.params import ParameterSpec, tunable_only, validate_params


def test_float_spec_with_bounds():
    spec = ParameterSpec(
        name="lookback",
        kind="float",
        default=0.5,
        bounds=(0.0, 1.0),
        description="decay factor",
    )
    assert spec.name == "lookback"
    assert spec.kind == "float"
    assert spec.bounds == (0.0, 1.0)
    assert spec.tunable is True


def test_int_spec():
    spec = ParameterSpec(name="window", kind="int", default=20, bounds=(5, 200))
    assert spec.kind == "int"
    assert spec.default == 20


def test_categorical_spec():
    spec = ParameterSpec(
        name="method",
        kind="categorical",
        default="ewm",
        bounds=["ewm", "sma", "median"],
    )
    assert spec.kind == "categorical"
    assert "sma" in spec.bounds


def test_bool_spec_allows_none_bounds():
    spec = ParameterSpec(name="use_adj_close", kind="bool", default=True, bounds=None)
    assert spec.kind == "bool"
    assert spec.bounds is None


def test_spec_is_frozen():
    spec = ParameterSpec(name="x", kind="int", default=1, bounds=(0, 10))
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.default = 2  # type: ignore[misc]


def test_tunable_flag_defaults_true_but_can_be_false():
    fixed = ParameterSpec(
        name="data_path",
        kind="categorical",
        default="/data",
        bounds=["/data"],
        tunable=False,
    )
    assert fixed.tunable is False


def test_tunable_only_filters_out_non_tunable():
    space = [
        ParameterSpec(name="a", kind="int", default=1, bounds=(0, 10), tunable=True),
        ParameterSpec(name="b", kind="int", default=2, bounds=(0, 10), tunable=False),
        ParameterSpec(name="c", kind="float", default=0.5, bounds=(0.0, 1.0), tunable=True),
    ]
    filtered = tunable_only(space)
    assert [s.name for s in filtered] == ["a", "c"]


def test_validate_params_accepts_valid():
    space = [
        ParameterSpec(name="n", kind="int", default=10, bounds=(1, 100)),
        ParameterSpec(name="w", kind="categorical", default="x", bounds=["x", "y"]),
    ]
    params = {"n": 20, "w": "y"}
    assert validate_params(params, space) is None  # should not raise
    assert params == {"n": 20, "w": "y"}  # and leaves the params alone


def test_validate_params_rejects_unknown_key():
    space = [ParameterSpec(name="n", kind="int", default=10, bounds=(1, 100))]
    with pytest.raises(ValueError, match="unknown"):
        validate_params({"nope": 1}, space)


def test_validate_params_rejects_out_of_bounds_numeric():
    space = [ParameterSpec(name="n", kind="int", default=10, bounds=(1, 100))]
    with pytest.raises(ValueError, match="out of bounds"):
        validate_params({"n": 999}, space)


def test_validate_params_rejects_wrong_categorical():
    space = [
        ParameterSpec(name="w", kind="categorical", default="x", bounds=["x", "y"]),
    ]
    with pytest.raises(ValueError, match="not in choices"):
        validate_params({"w": "z"}, space)


def test_validate_params_rejects_kind_mismatch():
    space = [ParameterSpec(name="n", kind="int", default=10, bounds=(1, 100))]
    with pytest.raises(ValueError, match="expected int"):
        validate_params({"n": "twenty"}, space)


def test_validate_params_uses_defaults_for_missing_tunable():
    """Missing tunable params should be allowed (filled with defaults by caller)."""
    space = [
        ParameterSpec(name="a", kind="int", default=1, bounds=(0, 10)),
        ParameterSpec(name="b", kind="int", default=2, bounds=(0, 10)),
    ]
    params = {"a": 5}
    assert validate_params(params, space) is None  # b is just missing
    assert "b" not in params  # the caller fills defaults, not the check


def test_numpy_scalars_are_accepted():
    # tuners and grids built with numpy hand over np.int64 / np.float64
    import numpy as np

    space = [
        ParameterSpec(name="n", kind="int", default=1, bounds=(0, 10)),
        ParameterSpec(name="x", kind="float", default=0.5, bounds=(0.0, 1.0)),
        ParameterSpec(name="flag", kind="bool", default=False),
    ]
    validate_params({"n": np.int64(5), "x": np.float64(0.25), "flag": np.bool_(True)}, space)
    validate_params({"x": np.int32(1)}, space)
    with pytest.raises(ValueError, match="expected int"):
        validate_params({"n": np.float64(5.0)}, space)
    with pytest.raises(ValueError, match="expected int"):
        validate_params({"n": np.bool_(True)}, space)
    with pytest.raises(ValueError, match="out of bounds"):
        validate_params({"n": np.int64(11)}, space)
