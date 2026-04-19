"""Unit tests for BaseStrategy default behavior."""

from __future__ import annotations

import json
from datetime import date

import pytest

from stonks.core.params import ParameterSpec
from stonks.strategies.base import BaseStrategy


class _MiniStrategy(BaseStrategy):
    id = "mini"

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(name="threshold", kind="float", default=0.1, bounds=(0.0, 1.0)),
            ParameterSpec(name="name", kind="categorical", default="a", bounds=["a", "b"]),
        ]

    def estimate_return(self, ticker, as_of, lake):
        return 0.5

    def decide(self, my_picks, portfolio, prices, as_of):
        return []


def test_base_strategy_validates_params_on_init():
    with pytest.raises(ValueError):
        _MiniStrategy({"threshold": 2.0})  # out of bounds


def test_base_strategy_fills_defaults_for_missing_tunable_params():
    s = _MiniStrategy({"threshold": 0.5})
    assert s.params["threshold"] == 0.5
    assert s.params["name"] == "a"  # default used


def test_base_strategy_fit_is_noop_by_default(tmp_path):
    s = _MiniStrategy({})
    s.fit(dataset=None)  # should not raise


def test_base_strategy_extract_features_returns_empty_by_default():
    s = _MiniStrategy({})
    out = s.extract_features(ticker="AAPL.US", as_of=date(2026, 4, 1), lake=None)
    assert out.values == {}


def test_base_strategy_save_load_roundtrips_params(tmp_path):
    s = _MiniStrategy({"threshold": 0.25, "name": "b"})
    s.save(tmp_path)
    loaded = _MiniStrategy.load(tmp_path)
    assert loaded.params["threshold"] == 0.25
    assert loaded.params["name"] == "b"
    # artifact bundle includes params.json
    assert json.loads((tmp_path / "params.json").read_text())["threshold"] == 0.25


def test_subclass_without_parameter_spec_treats_as_empty():
    class _Empty(BaseStrategy):
        id = "empty"

        def estimate_return(self, ticker, as_of, lake):
            return None

        def decide(self, my_picks, portfolio, prices, as_of):
            return []

    e = _Empty({})
    assert e.params == {}
