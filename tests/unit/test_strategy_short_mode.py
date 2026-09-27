"""The ``short_mode`` param of short-capable strategies (roadmap 16.3)."""

from __future__ import annotations

from typing import ClassVar

import pytest

from stonks.core.params import ParameterSpec
from stonks.strategies.base import BaseStrategy


class _Capable(BaseStrategy):
    id = "capable_test"
    short_capable: ClassVar[bool] = True

    @classmethod
    def parameter_spec(cls):
        return [ParameterSpec(name="n", kind="int", default=3, bounds=(1, 10))]


class _Plain(BaseStrategy):
    id = "plain_test"


class _Declared(BaseStrategy):
    id = "declared_test"
    supports_short = True


def test_short_mode_defaults_to_flat_and_leaves_params_unchanged():
    s = _Capable({})
    assert s.params == {"n": 3}
    assert s.short_mode == "flat"
    assert s.supports_short is False


def test_short_mode_short_turns_supports_short_on():
    s = _Capable({"short_mode": "short"})
    assert s.params["short_mode"] == "short"
    assert s.short_mode == "short"
    assert s.supports_short is True


def test_short_mode_rejects_unknown_values():
    with pytest.raises(ValueError, match="short_mode"):
        _Capable({"short_mode": "sideways"})


def test_short_mode_needs_a_short_capable_strategy():
    with pytest.raises(ValueError, match="short_mode"):
        _Plain({"short_mode": "short"})


def test_flat_short_mode_is_accepted_everywhere():
    assert _Plain({"short_mode": "flat"}).supports_short is False


def test_class_level_supports_short_is_kept():
    assert _Declared({}).supports_short is True
    assert _Declared({}).short_mode == "short"


def test_short_mode_survives_save_and_load(tmp_path):
    s = _Capable({"short_mode": "short"})
    s.save(tmp_path)
    loaded = _Capable.load(tmp_path)
    assert loaded.supports_short is True
