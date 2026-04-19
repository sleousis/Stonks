"""Unit tests for the (optional) features/library helpers.

These are *tools* a strategy can reach for. They are not a pipeline stage; a
strategy that prefers to compute everything inline is equally welcome.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from stonks.features.library import (
    pct_change_n_days,
    rolling_zscore,
    trailing_return,
    ttm,
)


def test_ttm_sums_last_four_periods():
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    out = ttm(s)
    assert pd.isna(out.iloc[0])
    assert out.iloc[3] == 10.0  # 1+2+3+4
    assert out.iloc[4] == 14.0  # 2+3+4+5


def test_ttm_passthrough_for_short_series():
    s = pd.Series([1.0, 2.0])
    assert ttm(s).isna().all()


def test_pct_change_n_days():
    s = pd.Series([100.0, 101.0, 102.0, 110.0, 105.0])
    out = pct_change_n_days(s, n=1)
    assert pd.isna(out.iloc[0])
    assert out.iloc[1] == pytest_approx(0.01)
    assert out.iloc[4] == pytest_approx(105.0 / 110.0 - 1.0)


def test_rolling_zscore_centered_around_zero_for_stationary():
    s = pd.Series(np.random.default_rng(42).normal(size=200))
    z = rolling_zscore(s, window=20)
    tail = z.iloc[-50:]
    assert abs(tail.mean()) < 0.5
    assert 0.5 < tail.std() < 2.0


def test_trailing_return_over_n_days():
    prices = pd.Series(
        [100.0, 102.0, 104.0, 106.0, 110.0],
        index=pd.date_range("2026-01-01", periods=5),
    )
    r = trailing_return(prices, n_days=2)
    # At index 2: 104/100 - 1 = 0.04
    assert r.iloc[2] == pytest_approx(0.04)
    # At index 4: 110/104 - 1
    assert r.iloc[4] == pytest_approx(110.0 / 104.0 - 1.0)


# --- tiny approx helper (avoids importing pytest namespace at module level) --


def pytest_approx(v: float, tol: float = 1e-9):
    return _Approx(v, tol)


class _Approx:
    def __init__(self, v, tol):
        self._v = v
        self._tol = tol

    def __eq__(self, other):
        return abs(other - self._v) <= self._tol

    def __repr__(self):
        return f"~{self._v}"
