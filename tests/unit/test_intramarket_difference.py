"""Unit tests for IntramarketDifferenceStrategy."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from stonks.features.indicators import atr
from stonks.strategies.examples.intramarket_difference import (
    IntramarketDifferenceStrategy,
    cmma,
    long_flat_states,
)
from tests.nt888_bars import as_of, bars, seed_lake, timestamps

T, REF = "ETH.CC", "BTC.CC"
PARAMS = {
    "ticker": T,
    "reference_ticker": REF,
    "lookback": 6,
    "atr_lookback": 24,
    "threshold": 0.25,
}


def _pair(n_calm=100):
    rng = np.random.default_rng(9)
    common = rng.normal(0, 0.002, n_calm + 30).cumsum()
    ref = 50 * np.exp(common)
    traded = 100 * np.exp(common + rng.normal(0, 0.0005, n_calm + 30))
    # traded outperforms for 6 bars, then gives it all back and more
    traded[n_calm : n_calm + 6] *= np.linspace(1.01, 1.06, 6)
    traded[n_calm + 6 : n_calm + 18] *= 1.06
    traded[n_calm + 18 :] *= 0.97
    return bars(traded), bars(ref)


def test_spec():
    specs = {s.name: s for s in IntramarketDifferenceStrategy.parameter_spec()}
    assert (specs["lookback"].default, specs["lookback"].bounds) == (24, (6, 168))
    assert (specs["threshold"].default, specs["threshold"].bounds) == (0.25, (0.05, 1.0))
    assert (specs["atr_lookback"].default, specs["atr_lookback"].bounds) == (168, (24, 336))
    assert specs["reference_ticker"].default == "BTC-USD.CC"
    assert specs["reference_ticker"].tunable is False


def test_cmma_hand_computed():
    frame = bars(np.linspace(100, 130, 40))
    h, lo, c = frame["high"], frame["low"], frame["close"]
    got = cmma(h, lo, c, lookback=5, atr_lookback=10)
    expected = (c - c.rolling(5).mean()) / (atr(h, lo, c, 10) * np.sqrt(5))
    assert np.allclose(got, expected, equal_nan=True)
    assert got.iloc[:10].isna().all()


def test_cmma_zero_atr_is_nan_not_inf():
    frame = bars(np.full(40, 100.0), spread=0.0)
    got = cmma(frame["high"], frame["low"], frame["close"], 5, 10)
    assert not np.isinf(got).any()


def test_long_flat_state_machine():
    diff = np.array([np.nan, 0.1, 0.3, 0.2, 0.01, -0.1, 0.2, 0.5, 0.0])
    assert long_flat_states(diff, 0.25).tolist() == [0, 0, 1, 1, 1, 0, 0, 1, 0]


def test_long_on_outperformance_then_flat(tmp_path):
    traded, ref = _pair()
    lake = seed_lake(tmp_path / "lake.duckdb", {T: traded, REF: ref})
    try:
        s = IntramarketDifferenceStrategy(PARAMS)
        assert s.estimate_return(T, as_of(traded, 95), lake) is None
        hits = {i: s.estimate_return(T, as_of(traded, i), lake) for i in range(100, 130)}
        assert any(r is not None and r > 0 for i, r in hits.items() if i < 106)
        assert hits[129] is None  # underperformance flattened it
        f = s.extract_features(T, as_of(traded, 103), lake).values
        assert f["diff"] == pytest.approx(f["cmma_traded"] - f["cmma_reference"])
    finally:
        lake.close()


def test_missing_reference_returns_none(tmp_path):
    traded, _ = _pair()
    lake = seed_lake(tmp_path / "lake.duckdb", {T: traded})
    try:
        s = IntramarketDifferenceStrategy(PARAMS)
        assert s.estimate_return(T, as_of(traded, -1), lake) is None
        assert s.extract_features(T, as_of(traded, -1), lake).values == {}
    finally:
        lake.close()


def test_stale_reference_returns_none(tmp_path):
    traded, ref = _pair()
    lake = seed_lake(tmp_path / "lake.duckdb", {T: traded, REF: ref.iloc[:-1]})
    try:
        s = IntramarketDifferenceStrategy(PARAMS)
        assert s.extract_features(T, as_of(traded, -1), lake).values == {}
        assert s.extract_features(T, as_of(traded, -2), lake).values != {}
    finally:
        lake.close()


def test_aligns_on_timestamps_not_positions(tmp_path):
    traded, ref = _pair()
    # extra reference bars at half-hours the traded ticker never printed
    extra = bars(
        ref["close"].to_numpy()[:60] * 1.5,
        ts=[t + timedelta(minutes=30) for t in timestamps(60)],
    )
    noisy_ref = pd.concat([ref, extra]).sort_values("timestamp").reset_index(drop=True)
    lake_a = seed_lake(tmp_path / "a.duckdb", {T: traded, REF: ref})
    lake_b = seed_lake(tmp_path / "b.duckdb", {T: traded, REF: noisy_ref})
    try:
        a = IntramarketDifferenceStrategy(PARAMS)
        b = IntramarketDifferenceStrategy(PARAMS)
        for i in (60, 90, 103, 120):
            fa = a.extract_features(T, as_of(traded, i), lake_a).values
            fb = b.extract_features(T, as_of(traded, i), lake_b).values
            assert fa == pytest.approx(fb, nan_ok=True), i
    finally:
        lake_a.close()
        lake_b.close()


def test_gap_in_reference_drops_the_bar_from_both(tmp_path):
    traded, ref = _pair()
    gappy = ref.drop(index=[50, 51]).reset_index(drop=True)
    lake = seed_lake(tmp_path / "lake.duckdb", {T: traded, REF: gappy})
    try:
        f = IntramarketDifferenceStrategy(PARAMS).extract_features(T, as_of(traded, -1), lake)
        # the replay window is the last 4 * atr_lookback traded bars
        assert f.values["joined_bars"] == 4 * 24 - 2
    finally:
        lake.close()
