"""Unit tests for VSAStrategy (VSA deviation + Stonks' absorption rule)."""

from __future__ import annotations

import numpy as np
import pytest

from stonks.strategies.examples.vsa import VSAStrategy
from tests.nt888_bars import as_of, bars, seed_lake

T = "X.CC"
N = 150
SPIKE = 140


def _frame(mode: str):
    rng = np.random.default_rng(4)
    volume = rng.uniform(500, 1500, N)
    spread = volume / 1000 * 2.0 + rng.normal(0, 0.05, N)
    closes = 100 + rng.normal(0, 0.005, N).cumsum()
    if mode == "absorption":  # huge volume, tiny range
        volume[SPIKE], spread[SPIKE] = 3000.0, 0.1
    else:  # wide range, little volume
        volume[SPIKE], spread[SPIKE] = 400.0, 6.0
    return bars(closes, spread=spread, volume=volume)


def test_spec():
    specs = {s.name: s for s in VSAStrategy.parameter_spec()}
    assert (specs["norm_lookback"].default, specs["norm_lookback"].bounds) == (168, (48, 504))
    assert specs["threshold"].bounds == (0.5, 2.0)
    assert specs["hold_bars"].bounds == (1, 48)
    assert specs["mode"].default == "absorption"
    assert specs["mode"].tunable is False


@pytest.mark.parametrize("mode", ["absorption", "expansion"])
def test_enters_on_spike_and_exits_after_hold_bars(tmp_path, mode):
    frame = _frame(mode)
    lake = seed_lake(tmp_path / "lake.duckdb", {T: frame})
    try:
        s = VSAStrategy(
            {"ticker": T, "norm_lookback": 48, "threshold": 1.0, "hold_bars": 4, "mode": mode}
        )
        signals = [s.estimate_return(T, as_of(frame, i), lake) for i in range(SPIKE - 5, N)]
        longs = [i for i, r in zip(range(SPIKE - 5, N), signals, strict=True) if r is not None]
        assert longs == [SPIKE, SPIKE + 1, SPIKE + 2, SPIKE + 3]
        f = s.extract_features(T, as_of(frame, SPIKE + 2), lake).values
        assert f["bars_since_trigger"] == 2.0
        assert abs(f["trigger_dev"]) > 1.0
        assert s.estimate_return(T, as_of(frame, SPIKE + 2), lake) == pytest.approx(
            abs(f["trigger_dev"])
        )
    finally:
        lake.close()


def test_absorption_mode_ignores_expansion_spike(tmp_path):
    frame = _frame("expansion")
    lake = seed_lake(tmp_path / "lake.duckdb", {T: frame})
    try:
        s = VSAStrategy({"ticker": T, "norm_lookback": 48, "threshold": 1.0, "hold_bars": 4})
        assert all(s.estimate_return(T, as_of(frame, i), lake) is None for i in range(96, N))
    finally:
        lake.close()


def test_none_until_two_norm_lookbacks_of_history(tmp_path):
    frame = _frame("absorption")
    lake = seed_lake(tmp_path / "lake.duckdb", {T: frame})
    try:
        s = VSAStrategy({"ticker": T, "norm_lookback": 48})
        assert s.extract_features(T, as_of(frame, 95), lake).values == {}
        assert s.extract_features(T, as_of(frame, 96), lake).values != {}
    finally:
        lake.close()


def test_missing_volume_gives_no_signal_not_an_error(tmp_path):
    frame = _frame("absorption")
    frame["volume"] = None
    lake = seed_lake(tmp_path / "lake.duckdb", {T: frame})
    try:
        s = VSAStrategy({"ticker": T, "norm_lookback": 48})
        assert s.estimate_return(T, as_of(frame, -1), lake) is None
        assert s.extract_features(T, as_of(frame, -1), lake).values == {}
    finally:
        lake.close()
