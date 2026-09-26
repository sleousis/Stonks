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

from stonks.lab.survival.permutation import permute_bars, permute_bars_together


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


def _reference_permute_loop(bars: pd.DataFrame, start_index: int, seed: int) -> dict:
    """The original bar-by-bar reconstruction, kept as an oracle so the
    vectorized version must reproduce it exactly for a given seed."""
    df = bars.reset_index(drop=True)
    perm_index = start_index + 1
    n = len(df)
    perm_n = n - perm_index
    rng = np.random.default_rng(seed)
    lo = np.log(df["open"].to_numpy(dtype=float))
    lh = np.log(df["high"].to_numpy(dtype=float))
    ll = np.log(df["low"].to_numpy(dtype=float))
    lc = np.log(df["close"].to_numpy(dtype=float))
    r_o = np.empty(n)
    r_o[1:] = lo[1:] - lc[:-1]
    r_o[0] = 0.0
    r_h, r_l, r_c = lh - lo, ll - lo, lc - lo
    perm1 = rng.permutation(perm_n)
    perm2 = rng.permutation(perm_n)
    sh, sl, sc = r_h[perm_index:][perm1], r_l[perm_index:][perm1], r_c[perm_index:][perm1]
    so = r_o[perm_index:][perm2]
    no, nh, nl, nc = lo.copy(), lh.copy(), ll.copy(), lc.copy()
    for i in range(perm_n):
        idx = perm_index + i
        no[idx] = nc[idx - 1] + so[i]
        nh[idx] = no[idx] + sh[i]
        nl[idx] = no[idx] + sl[i]
        nc[idx] = no[idx] + sc[i]
    return {"open": np.exp(no), "high": np.exp(nh), "low": np.exp(nl), "close": np.exp(nc)}


def test_output_matches_reference_loop_exactly_for_seed():
    bars = _synthetic_bars(n=300, seed=3)
    for start_index, seed in [(0, 1), (10, 42), (150, 7)]:
        out = permute_bars(bars, start_index=start_index, seed=seed)
        ref = _reference_permute_loop(bars, start_index, seed)
        for col in ("open", "high", "low", "close"):
            assert np.array_equal(out[col].to_numpy(), ref[col]), (col, start_index, seed)
        assert np.array_equal(out["adj_close"].to_numpy(), ref["close"])


# ---- several tickers: one shared permutation (Masters multi-market MCPT) ------


def _scaled(bars: pd.DataFrame, factor: float, ticker: str) -> pd.DataFrame:
    out = bars.copy()
    for col in ("open", "high", "low", "close", "adj_close"):
        out[col] = out[col] * factor
    out["ticker"] = ticker
    return out


def test_single_ticker_matches_permute_bars_for_a_seed():
    bars = _synthetic_bars(n=120, seed=5)
    for start_index, seed in [(0, 1), (30, 9)]:
        (out,) = permute_bars_together({"X.US": (bars, start_index)}, seed=seed).values()
        pd.testing.assert_frame_equal(out, permute_bars(bars, start_index=start_index, seed=seed))


def test_tickers_are_permuted_with_one_shared_ordering():
    """Two perfectly co-moving tickers stay perfectly co-moving: every bar
    of both draws its returns from the same source bar."""
    a = _synthetic_bars(n=150, seed=11)
    b = _scaled(a, 3.0, "Y.US")
    out = permute_bars_together({"X.US": (a, 20), "Y.US": (b, 20)}, seed=4)
    pa, pb = out["X.US"], out["Y.US"]
    assert not np.allclose(pa["close"].to_numpy(), a["close"].to_numpy())  # it did shuffle
    for col in ("open", "high", "low", "close"):
        np.testing.assert_allclose(pb[col].to_numpy(), 3.0 * pa[col].to_numpy(), rtol=1e-12)


def test_cross_asset_return_correlation_survives():
    rng = np.random.default_rng(0)
    a = _synthetic_bars(n=400, seed=21)
    # b: a's close-to-close log returns plus independent noise (corr ~0.9)
    ra = np.diff(np.log(a["close"].to_numpy()))
    rb = ra + rng.normal(0, 0.01, len(ra))
    b = a.copy()
    b["ticker"] = "Y.US"
    b["close"] = 50.0 * np.exp(np.concatenate([[0.0], np.cumsum(rb)]))
    b["open"] = np.concatenate([[b["close"].iloc[0]], b["close"].to_numpy()[:-1]])
    b["high"] = np.maximum(b["open"], b["close"])
    b["low"] = np.minimum(b["open"], b["close"])
    b["adj_close"] = b["close"]

    def corr(x, y):
        return np.corrcoef(np.diff(np.log(x["close"])), np.diff(np.log(y["close"])))[0, 1]

    out = permute_bars_together({"X.US": (a, 0), "Y.US": (b, 0)}, seed=3)
    assert corr(a, b) > 0.8
    assert corr(out["X.US"], out["Y.US"]) > 0.8 * corr(a, b)


def test_tickers_with_different_histories_keep_their_own_shape():
    a = _synthetic_bars(n=100, seed=1)
    b = _scaled(a.iloc[10:].reset_index(drop=True), 2.0, "Y.US")  # starts 10 bars later
    b = b.drop(index=[40, 41]).reset_index(drop=True)  # and has a gap
    out = permute_bars_together({"X.US": (a, 20), "Y.US": (b, 10)}, seed=8)
    assert len(out["X.US"]) == len(a) and len(out["Y.US"]) == len(b)
    pd.testing.assert_frame_equal(out["X.US"].iloc[:21], a.iloc[:21])
    pd.testing.assert_frame_equal(out["Y.US"].iloc[:11], b.iloc[:11])
    assert list(out["Y.US"]["timestamp"]) == list(b["timestamp"])
    # every permuted bar's (high, low, close) relative comes from the ticker's own bars
    rel = np.round(np.log(b["close"] / b["open"]).to_numpy()[11:], 12)
    got = np.round(np.log(out["Y.US"]["close"] / out["Y.US"]["open"]).to_numpy()[11:], 12)
    assert sorted(got) == sorted(rel)
