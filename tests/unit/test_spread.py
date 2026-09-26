"""Unit tests for the OHLC spread estimators (``stonks.features.spread``)."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from stonks.features.spread import (
    abdi_ranaldo,
    corwin_schultz,
    corwin_schultz_pairs,
    half_spread_bps,
)

# Three bars whose closes sit inside the next bar's range (no gap adjustment).
_H = pd.Series([101.0, 101.2, 101.1])
_L = pd.Series([99.0, 99.2, 99.0])
_C = pd.Series([100.0, 100.2, 100.0])
# Pinned by hand from Corwin & Schultz (2012) eqs. (14)-(18).
_PAIR_1 = 0.015156361960876424
_PAIR_2 = 0.016867675774829193


def _bounce(n: int = 60, bid: float = 99.9, ask: float = 100.1) -> tuple[pd.Series, ...]:
    """A constant mid with the close bouncing between bid and ask; each
    bar's range is exactly the quoted spread."""
    close = pd.Series([ask if i % 2 else bid for i in range(n)])
    return pd.Series([ask] * n), pd.Series([bid] * n), close


def test_corwin_schultz_two_bar_estimates_match_the_paper_by_hand():
    pairs = corwin_schultz_pairs(_H, _L, _C)
    assert math.isnan(pairs.iloc[0])
    assert pairs.iloc[1] == pytest.approx(_PAIR_1, rel=1e-12)
    assert pairs.iloc[2] == pytest.approx(_PAIR_2, rel=1e-12)


def test_corwin_schultz_averages_the_last_n_pairs():
    s = corwin_schultz(_H, _L, _C, n=2)
    assert s.iloc[:2].isna().all()
    assert s.iloc[2] == pytest.approx((_PAIR_1 + _PAIR_2) / 2, rel=1e-12)


def test_corwin_schultz_floors_negative_pair_estimates_at_zero():
    # a wide overnight move with narrow ranges -> alpha < 0
    high = pd.Series([102.0, 103.0])
    low = pd.Series([100.0, 101.0])
    assert corwin_schultz_pairs(high, low).iloc[1] == 0.0


def test_corwin_schultz_shifts_a_gapped_bar_by_the_overnight_move():
    # bar 2 gaps up by 10 over the prior close: with the close the gap is
    # removed, so the pair looks like two overlapping identical ranges
    high = pd.Series([101.0, 111.0])
    low = pd.Series([99.0, 109.0])
    close = pd.Series([99.0, 110.0])
    adjusted = corwin_schultz_pairs(high, low, close).iloc[1]
    same = corwin_schultz_pairs(pd.Series([101.0, 101.0]), pd.Series([99.0, 99.0])).iloc[1]
    assert adjusted == pytest.approx(same)
    assert corwin_schultz_pairs(high, low).iloc[1] == 0.0


@pytest.mark.parametrize("estimator", ["cs", "ar"])
def test_both_estimators_recover_a_pure_bid_ask_bounce(estimator):
    high, low, close = _bounce()
    fn = corwin_schultz if estimator == "cs" else abdi_ranaldo
    s = fn(high, low, close, n=20)
    true = math.log(100.1 / 99.9)
    assert s.iloc[-1] == pytest.approx(true, rel=1e-3)


@pytest.mark.parametrize("fn", [corwin_schultz, abdi_ranaldo])
def test_estimators_are_causal(fn):
    rng = np.random.default_rng(3)
    close = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.01, 80))))
    high = close * (1 + rng.uniform(0, 0.01, 80))
    low = close * (1 - rng.uniform(0, 0.01, 80))
    base = fn(high, low, close, n=10)
    high2, low2, close2 = high.copy(), low.copy(), close.copy()
    high2.iloc[50:] *= 1.3
    low2.iloc[50:] *= 0.7
    close2.iloc[50:] *= 1.1
    changed = fn(high2, low2, close2, n=10)
    pd.testing.assert_series_equal(base.iloc[:50], changed.iloc[:50])


def test_estimators_work_column_wise_on_frames():
    high, low, close = _bounce()
    frame = abdi_ranaldo(
        pd.DataFrame({"A": high, "B": high}),
        pd.DataFrame({"A": low, "B": low}),
        pd.DataFrame({"A": close, "B": close}),
        n=5,
    )
    assert list(frame.columns) == ["A", "B"]
    pd.testing.assert_series_equal(
        frame["A"], abdi_ranaldo(high, low, close, n=5), check_names=False
    )


def test_abdi_ranaldo_is_zero_without_bounce():
    n = 30
    close = pd.Series(np.linspace(100, 110, n))
    s = abdi_ranaldo(close * 1.001, close * 0.999, close, n=10)
    assert s.iloc[-1] == pytest.approx(0.0, abs=1e-12)


def test_window_must_be_positive():
    with pytest.raises(ValueError):
        corwin_schultz(_H, _L, n=0)
    with pytest.raises(ValueError):
        abdi_ranaldo(_H, _L, _C, n=0)


def test_half_spread_bps():
    assert half_spread_bps(0.002) == pytest.approx(10.0)
