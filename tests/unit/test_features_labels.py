"""Triple-barrier labels, sample uniqueness and the sequential bootstrap (BL-45)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stonks.features.labels import (
    avg_uniqueness,
    concurrency,
    ewma_vol,
    sequential_bootstrap,
    triple_barrier,
)


def _series(values) -> pd.Series:
    return pd.Series(values, index=pd.date_range("2024-01-01", periods=len(values), freq="D"))


# ---- volatility -------------------------------------------------------------


def test_ewma_vol_of_alternating_returns_is_their_size():
    r = 0.01
    log_path = np.cumsum([0.0] + [r if i % 2 else -r for i in range(400)])
    vol = ewma_vol(_series(np.exp(log_path)), span=100)
    assert vol.iloc[-1] == pytest.approx(r, rel=0.02)
    assert np.isnan(vol.iloc[0])


def test_ewma_vol_is_causal():
    rng = np.random.default_rng(0)
    close = _series(np.exp(np.cumsum(rng.normal(0, 0.01, 300))))
    full = ewma_vol(close, span=20)
    head = ewma_vol(close.iloc[:150], span=20)
    pd.testing.assert_series_equal(full.iloc[:150], head)


# ---- triple barrier ---------------------------------------------------------------


def test_rising_path_hits_the_upper_barrier():
    close = _series(np.exp(np.arange(30) * 0.01))
    vol = pd.Series(0.02, index=close.index)
    out = triple_barrier(close, [close.index[5]], tp_mult=1.0, sl_mult=1.0, max_hold=10, vol=vol)
    row = out.iloc[0]
    assert row["label"] == 1 and row["barrier"] == "tp"
    assert row["t1"] == close.index[7]  # two steps of 0.01 reach +0.02
    assert row["ret"] == pytest.approx(0.02)
    assert bool(row["complete"])


def test_falling_path_hits_the_lower_barrier():
    close = _series(np.exp(-np.arange(30) * 0.01))
    vol = pd.Series(0.02, index=close.index)
    out = triple_barrier(close, [5], tp_mult=1.0, sl_mult=1.5, max_hold=10, vol=vol)
    row = out.iloc[0]
    assert row["label"] == -1 and row["barrier"] == "sl"
    assert row["t1"] == close.index[8]


def test_flat_path_ends_at_the_vertical_barrier():
    close = _series(np.full(30, 100.0))
    vol = pd.Series(0.02, index=close.index)
    out = triple_barrier(close, [5], max_hold=4, vol=vol)
    row = out.iloc[0]
    assert row["label"] == 0 and row["barrier"] == "time"
    assert row["t1"] == close.index[9]
    assert row["ret"] == pytest.approx(0.0)


def test_high_and_low_touches_count_and_a_double_touch_is_a_loss():
    close = _series(np.full(10, 100.0))
    high = close.copy()
    low = close.copy()
    high.iloc[3] = 103.0  # touches +2% on bar 3
    vol = pd.Series(0.02, index=close.index)
    out = triple_barrier(close, [1], max_hold=5, vol=vol, high=high, low=low)
    row = out.iloc[0]
    assert row["barrier"] == "tp" and row["t1"] == close.index[3]
    assert row["ret"] == pytest.approx(0.02)  # filled at the barrier
    low.iloc[3] = 97.0
    out = triple_barrier(close, [1], max_hold=5, vol=vol, high=high, low=low)
    assert out.iloc[0]["barrier"] == "sl"
    assert out.iloc[0]["ret"] == pytest.approx(-0.02)


def test_events_near_the_end_are_marked_incomplete():
    close = _series(np.full(10, 100.0))
    vol = pd.Series(0.02, index=close.index)
    out = triple_barrier(close, [7], max_hold=5, vol=vol)
    assert not bool(out.iloc[0]["complete"])
    assert out.iloc[0]["t1"] == close.index[9]


def test_events_without_volatility_are_skipped_and_default_vol_is_ewma():
    rng = np.random.default_rng(1)
    close = _series(np.exp(np.cumsum(rng.normal(0, 0.01, 200))))
    out = triple_barrier(close, [0, 50, 120], max_hold=10, vol_span=20)
    # the first bar has no volatility estimate yet
    assert list(out["t0_pos"]) == [50, 120]
    assert set(out.columns) >= {"t1", "t1_pos", "ret", "label", "barrier", "complete", "vol"}


def test_triple_barrier_rejects_bad_arguments():
    close = _series(np.full(10, 100.0))
    with pytest.raises(ValueError):
        triple_barrier(close, [1], max_hold=0)
    with pytest.raises(ValueError):
        triple_barrier(close, [1], tp_mult=0.0)
    with pytest.raises(KeyError):
        triple_barrier(close, [pd.Timestamp("1999-01-01")])


def test_triple_barrier_with_no_events():
    close = _series(np.full(10, 100.0))
    out = triple_barrier(close, [])
    assert out.empty and "label" in out.columns


# ---- uniqueness -------------------------------------------------------------------


def test_concurrency_counts_open_labels_per_bar():
    c = concurrency([0, 2], [2, 4], n=6)
    assert c.tolist() == [1, 1, 2, 1, 1, 0]


def test_average_uniqueness_known_cases():
    assert avg_uniqueness([0, 5], [2, 7]).tolist() == [1.0, 1.0]
    assert avg_uniqueness([0, 0], [3, 3]).tolist() == [0.5, 0.5]
    u = avg_uniqueness([0, 2], [2, 4])
    assert u == pytest.approx([(1 + 1 + 0.5) / 3, (0.5 + 1 + 1) / 3])


def test_uniqueness_rejects_bad_spans():
    with pytest.raises(ValueError):
        avg_uniqueness([3], [1])
    with pytest.raises(ValueError):
        avg_uniqueness([0, 1], [1])


def test_sequential_bootstrap_is_seeded_and_prefers_unique_events():
    t0 = [0, 0, 0, 10]
    t1 = [5, 5, 5, 15]  # three identical events, one unique
    a = sequential_bootstrap(t0, t1, n_draws=4000, seed=3)
    b = sequential_bootstrap(t0, t1, n_draws=4000, seed=3)
    assert np.array_equal(a, b)
    counts = np.bincount(a, minlength=4)
    # a uniform bootstrap would draw the unique event a quarter of the time
    assert counts[3] / len(a) > 0.3
    assert len(sequential_bootstrap(t0, t1, seed=0)) == 4


def test_a_passed_vol_is_aligned_by_date_not_position():
    """A vol series on a shorter index (``ewma_vol(...).dropna()``) must give
    each event its own day's width, never a later bar's (look-ahead)."""
    rng = np.random.default_rng(3)
    idx = pd.bdate_range("2025-01-01", periods=40)
    close = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.01, 40))), index=idx)
    vol = pd.Series(np.linspace(0.01, 0.05, 40), index=idx)
    full = triple_barrier(close, [idx[20]], vol=vol, max_hold=5)
    trimmed = triple_barrier(close, [idx[20]], vol=vol.iloc[2:], max_hold=5)
    assert trimmed["vol"].iloc[0] == pytest.approx(vol.iloc[20])
    pd.testing.assert_frame_equal(trimmed, full)
