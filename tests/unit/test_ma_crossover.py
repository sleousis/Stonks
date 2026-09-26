"""Unit tests for MACrossoverStrategy (neurotrader888/mcpt moving average)."""

from __future__ import annotations

import numpy as np
import pytest

from stonks.strategies.examples.ma_crossover import MACrossoverStrategy
from tests.nt888_bars import as_of, bars, seed_lake


@pytest.fixture
def lake(tmp_path):
    closes = np.concatenate([np.linspace(120, 100, 40), np.linspace(100, 130, 40)])
    frame = bars(closes)
    lk = seed_lake(tmp_path / "lake.duckdb", {"X.CC": frame})
    yield lk, frame
    lk.close()


def test_spec_bounds_and_defaults():
    specs = {s.name: s for s in MACrossoverStrategy.parameter_spec()}
    assert (specs["fast"].default, specs["fast"].bounds) == (10, (2, 50))
    assert (specs["slow"].default, specs["slow"].bounds) == (30, (10, 200))


def test_fast_must_be_below_slow():
    with pytest.raises(ValueError, match="fast.*slow"):
        MACrossoverStrategy({"fast": 20, "slow": 20})
    with pytest.raises(ValueError, match="fast.*slow"):
        MACrossoverStrategy({"fast": 40, "slow": 15})


def test_flat_in_downtrend_long_in_uptrend(lake):
    lk, frame = lake
    s = MACrossoverStrategy({"ticker": "X.CC", "fast": 3, "slow": 10})
    assert s.estimate_return("X.CC", as_of(frame, 35), lk) is None
    r = s.estimate_return("X.CC", as_of(frame, 75), lk)
    assert r is not None and r > 0
    f = s.extract_features("X.CC", as_of(frame, 75), lk).values
    assert r == pytest.approx(f["fast_ma"] / f["slow_ma"] - 1)
    assert f["fast_ma"] == pytest.approx(np.mean(frame["close"].iloc[73:76]))


def test_needs_slow_bars(lake):
    lk, frame = lake
    s = MACrossoverStrategy({"ticker": "X.CC", "fast": 3, "slow": 10})
    assert s.extract_features("X.CC", as_of(frame, 8), lk).values == {}
    assert s.extract_features("X.CC", as_of(frame, 9), lk).values != {}
