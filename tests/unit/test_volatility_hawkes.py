"""Unit tests for VolatilityHawkesStrategy."""

from __future__ import annotations

import numpy as np
import pytest

from stonks.strategies.examples.volatility_hawkes import (
    VolatilityHawkesStrategy,
    vol_breakout_states,
)
from tests.nt888_bars import as_of, bars, seed_lake

T = "X.CC"
PARAMS = {"ticker": T, "kappa": 0.5, "quantile_lookback": 24, "norm_lookback": 50}


def test_spec():
    specs = {s.name: s for s in VolatilityHawkesStrategy.parameter_spec()}
    assert (specs["kappa"].default, specs["kappa"].bounds) == (0.1, (0.01, 0.5))
    assert (specs["quantile_lookback"].default, specs["quantile_lookback"].bounds) == (
        168,
        (24, 336),
    )
    assert specs["norm_lookback"].default == 336
    assert specs["norm_lookback"].bounds == (50, 500)
    assert specs["norm_lookback"].tunable is False


def test_state_machine_on_hand_built_series():
    close = np.array([10, 10, 10, 11, 12, 12, 9, 9.0])
    v = np.array([5, 1, 5, 9, 9, 1, 5, 9.0])
    q05 = np.full(8, 2.0)
    q95 = np.full(8, 8.0)
    signal, last_below = vol_breakout_states(close, v, q05, q95)
    # bar 3 crosses above q95 with close above the calm bar 1 -> long;
    # bar 4 is still above q95 but not a fresh cross -> hold; bar 5 dips
    # below q05 -> flat; bar 7 crosses up with close below bar 5 -> the
    # original's short, flat here.
    assert signal.tolist() == [0, 0, 0, 1, 1, 0, 0, 0]
    assert last_below.tolist() == [-1, 1, 1, 1, 1, 5, 5, 5]


def test_state_machine_ignores_nan_warmup():
    close = np.array([10, 10, 11, 12.0])
    v = np.array([np.nan, 1, 9, 9.0])
    q05 = np.array([np.nan, 2, 2, 2.0])
    q95 = np.array([np.nan, 8, 8, 8.0])
    signal, _ = vol_breakout_states(close, v, q05, q95)
    assert signal.tolist() == [0, 0, 1, 1]


def test_no_entry_without_a_prior_calm_bar():
    close = np.array([10, 11, 12.0])
    v = np.array([5, 9, 5.0])
    signal, _ = vol_breakout_states(close, v, np.full(3, 2.0), np.full(3, 8.0))
    assert signal.tolist() == [0, 0, 0]


def _regime_series(direction: float):
    """200 normal bars, 15 very calm bars, then 12 wide bars trending in
    ``direction``."""
    rng = np.random.default_rng(0)
    n1, n2, n3 = 200, 15, 12
    c1 = 100 + rng.normal(0, 0.2, n1).cumsum()
    c2 = c1[-1] + rng.normal(0, 0.01, n2).cumsum()
    c3 = c2[-1] + direction * np.arange(1, n3 + 1) * 1.5
    closes = np.concatenate([c1, c2, c3])
    spread = np.concatenate([np.full(n1, 1.0), np.full(n2, 0.1), np.full(n3, 4.0)])
    return bars(closes, spread=spread)


@pytest.mark.parametrize(("direction", "long"), [(1.0, True), (-1.0, False)])
def test_volatility_burst_after_calm(tmp_path, direction, long):
    frame = _regime_series(direction)
    lake = seed_lake(tmp_path / "lake.duckdb", {T: frame})
    try:
        s = VolatilityHawkesStrategy(PARAMS)
        # calm phase: flat
        assert s.estimate_return(T, as_of(frame, 210), lake) is None
        hits = [s.estimate_return(T, as_of(frame, i), lake) for i in range(215, 227)]
        if long:
            assert any(h is not None for h in hits)
            f = s.extract_features(T, as_of(frame, -1), lake).values
            assert f["signal"] == 1.0
            r = s.estimate_return(T, as_of(frame, -1), lake)
            assert r == pytest.approx(f["close"] / f["close_at_last_below"] - 1)
        else:
            assert all(h is None for h in hits)
    finally:
        lake.close()


def test_zero_range_history_gives_no_signal_not_an_error(tmp_path):
    frame = bars(np.full(300, 100.0), spread=0.0)
    lake = seed_lake(tmp_path / "lake.duckdb", {T: frame})
    try:
        s = VolatilityHawkesStrategy(PARAMS)
        assert s.estimate_return(T, as_of(frame, -1), lake) is None
    finally:
        lake.close()
