"""Cross-instrument test (BL-19): options and registration. Behaviour on
real backtests: tests/integration/test_cross_instrument_survival.py."""

from __future__ import annotations

import pytest

from stonks.lab.survival.cross_instrument import CrossInstrumentOptions, CrossInstrumentTest
from stonks.lab.survival.registry import build_survival_test


def test_registry_builds_it_with_options():
    test = build_survival_test("cross_instrument", {"held_out": ["X.US"], "max_workers": 2})
    assert isinstance(test, CrossInstrumentTest)
    assert test.options.held_out == ["X.US"]
    assert test.options.max_workers == 2


def test_rejects_unknown_and_out_of_range_options():
    with pytest.raises(ValueError):
        build_survival_test("cross_instrument", {"nope": 1})
    with pytest.raises(ValueError):
        CrossInstrumentOptions(min_tickers=1)
    with pytest.raises(ValueError):
        CrossInstrumentOptions(held_out_auto=-1)


def test_keyword_overrides_are_validated():
    assert CrossInstrumentTest(min_positive_share=0.7).options.min_positive_share == 0.7
    with pytest.raises(ValueError):
        CrossInstrumentTest(min_positive_share=0.95)
