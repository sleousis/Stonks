"""Unit tests for shared volatility indicators (true range, ATR)."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from stonks.features.indicators import atr, true_range


def _bars() -> tuple[pd.Series, pd.Series, pd.Series]:
    high = pd.Series([10.0, 11.0, 12.0, 11.5, 13.0, 12.5])
    low = pd.Series([9.0, 9.5, 10.5, 10.0, 11.0, 11.5])
    close = pd.Series([9.5, 10.5, 11.0, 10.5, 12.5, 12.0])
    return high, low, close


def test_true_range_uses_previous_close_and_is_undefined_on_first_bar():
    high, low, close = _bars()
    tr = true_range(high, low, close)
    assert math.isnan(tr.iloc[0])
    # bar 1: max(11-9.5, |11-9.5|, |9.5-9.5|) = 1.5
    assert tr.iloc[1] == pytest.approx(1.5)
    # bar 4: max(13-11, |13-10.5|, |11-10.5|) = 2.5 (gap above previous close)
    assert tr.iloc[4] == pytest.approx(2.5)


def test_sma_atr_is_rolling_mean_of_true_range():
    high, low, close = _bars()
    tr = true_range(high, low, close)
    out = atr(high, low, close, 3, method="sma")
    assert math.isnan(out.iloc[2])
    assert out.iloc[3] == pytest.approx(tr.iloc[1:4].mean())
    assert out.iloc[5] == pytest.approx(tr.iloc[3:6].mean())


def test_rma_atr_is_wilder_smoothing_after_warmup():
    high, low, close = _bars()
    tr = true_range(high, low, close)
    out = atr(high, low, close, 3, method="rma")
    expected = tr.ewm(alpha=1 / 3, min_periods=3).mean()
    assert np.allclose(out.to_numpy(), expected.to_numpy(), equal_nan=True)
    assert out.iloc[:3].isna().all()
    assert not math.isnan(out.iloc[3])


def test_atr_is_causal():
    high, low, close = _bars()
    full = atr(high, low, close, 3)
    prefix = atr(high.iloc[:4], low.iloc[:4], close.iloc[:4], 3)
    assert np.allclose(full.iloc[:4].to_numpy(), prefix.to_numpy(), equal_nan=True)


def test_atr_rejects_bad_arguments():
    high, low, close = _bars()
    with pytest.raises(ValueError):
        atr(high, low, close, 0)
    with pytest.raises(ValueError):
        atr(high, low, close, 3, method="ema")
