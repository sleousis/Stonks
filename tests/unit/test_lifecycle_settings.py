"""Roadmap 22.6: ``[lifecycle]`` settings."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from stonks.config import load_settings
from stonks.lifecycle.settings import ModelLifecycleSettings, SwapPolicy


def test_default_config_carries_the_lifecycle_table():
    settings = load_settings().lifecycle
    assert settings == ModelLifecycleSettings()
    assert settings.swap.min_days == 20


def test_unknown_keys_and_bad_limits_are_refused():
    with pytest.raises(ValidationError):
        ModelLifecycleSettings.model_validate({"lookback": 10})
    with pytest.raises(ValidationError):
        SwapPolicy(max_drawdown=0.0)
    with pytest.raises(ValidationError):
        ModelLifecycleSettings(statuses=["retired"])  # type: ignore[list-item]
