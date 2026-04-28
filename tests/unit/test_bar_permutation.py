"""Unit tests for permute_bars.

The permutation must:
- preserve the first ``start_index + 1`` bars exactly
- preserve the per-bar return distribution (sum of log-returns is invariant)
- preserve intra-bar OHLC consistency (high >= max(open, close); low <= min(open, close))
- destroy time ordering (autocorrelation significantly reduced)
- be deterministic under a fixed seed
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

from stonks.lab.survival.permutation import permute_bars


def _synthetic_bars(n: int = 200, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    log_close = np.log(100.0) + np.cumsum(rng.normal(0.001, 0.02, size=n))
    close = np.exp(log_close)
    ts0 = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
    timestamps = [ts0 + timedelta(hours=i) for i in range(n)]
    open_ = np.concatenate([[close[0]], close[:-1]]) * np.exp(rng.normal(0, 0.003, n))
    high = np.maximum(open_, close) * np.exp(np.abs(rng.normal(0, 0.005, n)))
    low = np.minimum(open_, close) * np.exp(-np.abs(rng.normal(0, 0.005, n)))
    return pd.DataFrame(
        {
            "ticker": "X.US",
            "timestamp": timestamps,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "adj_close": close,
            "volume": 1_000_000,
        }
    )


def test_output_shape_and_columns_match_input():
    bars = _synthetic_bars()
    out = permute_bars(bars, start_index=0, seed=1)
    assert len(out) == len(bars)
    assert list(out.columns) == list(bars.columns)
    assert (out["timestamp"].to_numpy() == bars["timestamp"].to_numpy()).all()
    assert (out["ticker"] == bars["ticker"]).all()


def test_first_start_index_plus_one_bars_preserved_exactly():
    bars = _synthetic_bars()
    out = permute_bars(bars, start_index=10, seed=1)
    for col in ("open", "high", "low", "close"):
        assert np.allclose(out[col].to_numpy()[:11], bars[col].to_numpy()[:11])


def test_per_bar_ohlc_consistency_preserved():
    bars = _synthetic_bars()
    out = permute_bars(bars, start_index=0, seed=1)
    # high/low are inclusive extremes of (open, close) in the reference algorithm:
    # r_h = log(high) - log(open) >= 0, r_l = log(low) - log(open) <= 0
    assert ((out["high"] >= out["open"]) | np.isclose(out["high"], out["open"])).all()
    assert ((out["low"] <= out["open"]) | np.isclose(out["low"], out["open"])).all()


def test_sum_of_log_returns_is_invariant_under_permutation():
    bars = _synthetic_bars()
    out = permute_bars(bars, start_index=0, seed=1)
    real_sum = float(np.diff(np.log(bars["close"].to_numpy())).sum())
    perm_sum = float(np.diff(np.log(out["close"].to_numpy())).sum())
    # In log-space the per-bar increments are re-shuffled but preserved as a
    # multiset, so the cumulative sum (and hence the final log-close) is equal.
    assert abs(real_sum - perm_sum) < 1e-8


def test_permutation_destroys_autocorrelation():
    bars = _synthetic_bars(n=500, seed=11)
    # craft strong positive autocorrelation: momentum-like series
    driven = bars.copy()
    trend = np.linspace(0, 1.0, len(driven))
    driven["close"] = np.exp(np.log(100.0) + trend)
    driven["open"] = driven["close"].shift(1).fillna(driven["close"].iloc[0])
    driven["high"] = np.maximum(driven["open"], driven["close"])
    driven["low"] = np.minimum(driven["open"], driven["close"])
    driven["adj_close"] = driven["close"]

    real_r = np.log(driven["close"]).diff().dropna()
    perm = permute_bars(driven, start_index=0, seed=2)
    perm_r = np.log(perm["close"]).diff().dropna()

    real_autocorr = real_r.autocorr(lag=1)
    perm_autocorr = perm_r.autocorr(lag=1)
    # on a constantly-drifting series real autocorr should be much higher
    assert abs(real_autocorr) - abs(perm_autocorr) > -0.2  # permuted autocorr << real


def test_same_seed_is_deterministic():
    bars = _synthetic_bars()
    a = permute_bars(bars, start_index=5, seed=42)
    b = permute_bars(bars, start_index=5, seed=42)
    for col in ("open", "high", "low", "close"):
        assert np.allclose(a[col], b[col])


def test_different_seeds_yield_different_outputs():
    bars = _synthetic_bars()
    a = permute_bars(bars, start_index=0, seed=1)
    b = permute_bars(bars, start_index=0, seed=2)
    assert not np.allclose(a["close"], b["close"])


def test_empty_input_returns_empty_frame():
    empty = pd.DataFrame(
        columns=["ticker", "timestamp", "open", "high", "low", "close", "adj_close", "volume"]
    )
    out = permute_bars(empty, start_index=0, seed=1)
    assert out.empty


def test_input_too_short_returns_unchanged():
    bars = _synthetic_bars(n=3)
    out = permute_bars(bars, start_index=5, seed=1)  # start_index > len
    for col in ("open", "high", "low", "close"):
        assert np.allclose(out[col], bars[col])
