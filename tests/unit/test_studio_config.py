"""``[api] allow_code_strategies`` gates Strategy Studio code drafts."""

from __future__ import annotations

from stonks.config import ApiConfig


def test_code_strategies_are_off_by_default():
    assert ApiConfig().allow_code_strategies is False


def test_code_strategies_can_be_enabled_from_config():
    assert ApiConfig(allow_code_strategies=True).allow_code_strategies is True
