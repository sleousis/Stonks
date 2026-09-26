"""Unit tests for causal price-extreme detectors (``features.extremes``).

Every detector reports the bar an extreme is *confirmed* on as well as the
bar it sits at. The key property pinned here besides the hand-checked
values: an extreme is never visible before its confirmation bar, i.e. the
detector run on a prefix ``[:n]`` reports exactly the extremes of the full
run whose ``conf_index < n``.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from stonks.features.extremes import (
    ATRDirectionalChange,
    DirectionalChange,
    Extreme,
    HierarchicalExtremes,
    atr_directional_change,
    directional_change,
    find_pips,
    hierarchical_level_prices,
    rw_extremes,
    visible,
)
from stonks.features.indicators import atr


def _random_bars(n: int = 400, seed: int = 7) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.01, n)))
    spread = np.abs(rng.normal(0.0, 0.005, n)) * close
    return close + spread, close - spread, close


# ---- rolling window --------------------------------------------------------

ZIGZAG = np.array([1.0, 2.0, 3.0, 2.0, 1.0, 2.0, 3.0, 4.0, 3.0, 2.0])


def test_rw_extremes_finds_known_tops_and_bottoms():
    ext = rw_extremes(ZIGZAG, order=2)
    assert ext == [
        Extreme(conf_index=4, ext_index=2, price=3.0, kind="top"),
        Extreme(conf_index=6, ext_index=4, price=1.0, kind="bottom"),
        Extreme(conf_index=9, ext_index=7, price=4.0, kind="top"),
    ]


def test_rw_extreme_is_invisible_before_its_confirmation_bar():
    # the top at bar 7 needs bars 8 and 9 to be confirmed
    assert all(e.ext_index != 7 for e in rw_extremes(ZIGZAG[:9], order=2))
    assert any(e.ext_index == 7 for e in rw_extremes(ZIGZAG[:10], order=2))


def test_rw_extremes_confirmation_lag_equals_order():
    _, _, close = _random_bars()
    for e in rw_extremes(close, order=5):
        assert e.conf_index - e.ext_index == 5


@pytest.mark.parametrize("n", [15, 60, 123, 399])
def test_rw_extremes_prefix_is_causal(n):
    _, _, close = _random_bars()
    full = rw_extremes(close, order=4)
    assert rw_extremes(close[:n], order=4) == [e for e in full if e.conf_index < n]


def test_visible_filters_on_confirmation_index():
    ext = rw_extremes(ZIGZAG, order=2)
    assert visible(ext, 5) == ext[:1]
    assert visible(ext, 6) == ext[:2]
    assert visible(ext, 3) == []


# ---- directional change ----------------------------------------------------


def test_directional_change_hand_built_zigzag():
    close = np.array([100.0, 104.0, 110.0, 107.0, 104.0, 100.0, 103.0, 106.0, 108.0, 104.0])
    high = close + 1.0
    low = close - 1.0
    ext = directional_change(high, low, close, sigma=0.05)
    # top at bar 2 (high 111) confirms at bar 4: 104 < 111 * 0.95 = 105.45
    # bottom at bar 5 (low 99) confirms at bar 7: 106 > 99 * 1.05 = 103.95
    assert ext == [
        Extreme(conf_index=4, ext_index=2, price=111.0, kind="top"),
        Extreme(conf_index=7, ext_index=5, price=99.0, kind="bottom"),
    ]


def test_directional_change_incremental_matches_batch():
    high, low, close = _random_bars()
    dc = DirectionalChange(sigma=0.02)
    out = [e for i in range(len(close)) if (e := dc.update(i, high[i], low[i], close[i]))]
    assert out == directional_change(high, low, close, sigma=0.02)


@pytest.mark.parametrize("n", [50, 200, 399])
def test_directional_change_prefix_is_causal(n):
    high, low, close = _random_bars()
    full = directional_change(high, low, close, sigma=0.02)
    assert directional_change(high[:n], low[:n], close[:n], 0.02) == [
        e for e in full if e.conf_index < n
    ]


def test_directional_change_alternates():
    high, low, close = _random_bars()
    kinds = [e.kind for e in directional_change(high, low, close, sigma=0.02)]
    assert all(a != b for a, b in zip(kinds, kinds[1:], strict=False))


# ---- ATR directional change ------------------------------------------------


def test_atr_directional_change_uses_sma_atr_threshold():
    # constant 2.0-wide bars, no gaps => ATR = 2.0 once warmed up
    close = np.array([10.0] * 5 + [11.0, 12.0, 13.0, 14.0, 12.5, 11.5, 10.5, 12.0, 13.5])
    high = close + 1.0
    low = close - 1.0
    ext = atr_directional_change(high, low, close, atr_lookback=3)
    a = atr(pd.Series(high), pd.Series(low), pd.Series(close), 3, method="sma").to_numpy()
    assert math.isnan(a[2]) and not math.isnan(a[3])
    # pending max is the bar-8 high (15.0); bar 9 low 11.5 < 15 - atr[9]
    top = ext[0]
    assert (top.kind, top.ext_index, top.price) == ("top", 8, 15.0)
    assert top.conf_index == 9
    assert low[9] < 15.0 - a[9]
    # the ATR warm-up bars (0..2) never confirm anything
    assert all(e.conf_index >= 3 for e in ext)


@pytest.mark.parametrize("n", [60, 250, 399])
def test_atr_directional_change_prefix_is_causal(n):
    high, low, close = _random_bars()
    full = atr_directional_change(high, low, close, atr_lookback=14)
    assert atr_directional_change(high[:n], low[:n], close[:n], 14) == [
        e for e in full if e.conf_index < n
    ]


def test_atr_directional_change_incremental_takes_scalars_only():
    high, low, close = _random_bars()
    a = atr(pd.Series(high), pd.Series(low), pd.Series(close), 14, method="sma").to_numpy()
    dc = ATRDirectionalChange()
    out = [e for i in range(len(close)) if (e := dc.update(i, high[i], low[i], a[i]))]
    assert out == atr_directional_change(high, low, close, atr_lookback=14)


# ---- hierarchical extremes -------------------------------------------------


def _feed(he: HierarchicalExtremes, pts: list[tuple[str, float]]) -> None:
    """Feed alternating base extremes; extreme k sits at bar 2k, confirmed at 2k+1."""
    for k, (kind, price) in enumerate(pts):
        he.add_base_extreme(Extreme(2 * k + 1, 2 * k, price, kind))  # type: ignore[arg-type]


def test_hierarchical_promotes_high_that_beats_both_neighbours():
    he = HierarchicalExtremes(levels=3)
    _feed(he, [("top", 10.0), ("bottom", 5.0), ("top", 12.0), ("bottom", 6.0)])
    assert he.level_high(1) is None  # right-hand neighbour not seen yet
    he.add_base_extreme(Extreme(9, 8, 11.0, "top"))
    promoted = he.level_high(1)
    assert promoted is not None
    assert (promoted.ext_index, promoted.price) == (4, 12.0)
    # confirmed when the right neighbour confirmed, not when it was set
    assert promoted.conf_index == 9


def test_hierarchical_inserts_opposite_extreme_to_keep_alternation():
    he = HierarchicalExtremes(levels=3)
    pts = [
        ("top", 10.0),
        ("bottom", 5.0),
        ("top", 12.0),
        ("bottom", 6.0),
        ("top", 11.0),  # promotes 12 (level-1 high)
        ("bottom", 6.5),  # rising lows: no level-1 low gets promoted
        ("top", 13.0),
        ("bottom", 7.0),
        ("top", 12.5),  # promotes 13: needs a level-1 low between 12 and 13
    ]
    _feed(he, pts)
    lvl1 = he.extremes[1]
    assert [(e.kind, e.price) for e in lvl1] == [
        ("top", 12.0),
        ("bottom", 6.0),  # lowest base low between the two level-1 highs
        ("top", 13.0),
    ]
    assert all(e.conf_index == 17 for e in lvl1[1:])
    assert he.level_low_price(1) == 6.0
    assert he.level_high_price(1) == 13.0
    assert he.level_high_price(1, lag=1) == 12.0
    assert math.isnan(he.level_high_price(2))


def test_hierarchical_level_prices_are_causal():
    high, low, close = _random_bars(600, seed=11)
    hi_full, lo_full = hierarchical_level_prices(high, low, close, atr_lookback=10, level=1)
    hi_pre, lo_pre = hierarchical_level_prices(
        high[:350], low[:350], close[:350], atr_lookback=10, level=1
    )
    np.testing.assert_array_equal(hi_pre, hi_full[:350])
    np.testing.assert_array_equal(lo_pre, lo_full[:350])
    assert np.isfinite(hi_full[-1]) and np.isfinite(lo_full[-1])


def test_hierarchical_level0_matches_atr_directional_change():
    high, low, close = _random_bars()
    base = atr_directional_change(high, low, close, atr_lookback=14)
    hi, _lo = hierarchical_level_prices(high, low, close, atr_lookback=14, level=0)
    tops = [e for e in base if e.kind == "top"]
    assert hi[-1] == tops[-1].price
    for prev, cur in zip(tops, tops[1:], strict=False):
        assert hi[cur.conf_index] == cur.price
        assert hi[cur.conf_index - 1] == prev.price  # not visible one bar early
    assert math.isnan(hi[tops[0].conf_index - 1])


# ---- perceptually important points -----------------------------------------


def test_find_pips_picks_the_spike_first():
    data = np.array([0.0, 1.0, 2.0, 3.0, 10.0, 3.0, 2.0, 1.0, 0.0])
    x, y = find_pips(data, n_pips=3, dist_measure=3)
    assert x == [0, 4, 8]
    assert y == [0.0, 10.0, 0.0]


@pytest.mark.parametrize("measure", [1, 2, 3])
def test_find_pips_returns_sorted_unique_points_including_endpoints(measure):
    _, _, close = _random_bars(60)
    x, y = find_pips(close, n_pips=7, dist_measure=measure)
    assert len(x) == 7
    assert x[0] == 0 and x[-1] == 59
    assert x == sorted(set(x))
    assert y == [close[i] for i in x]


def test_find_pips_distance_measures_can_disagree():
    # vertical distance picks the (slightly) taller bump in the middle;
    # euclidean distance (sum of distances to both neighbours) grows near
    # an endpoint and picks the bump next to the left end
    data = np.zeros(16)
    data[1] = 1.0
    data[8] = 1.05
    xv, _ = find_pips(data, 3, dist_measure=3)
    xe, _ = find_pips(data, 3, dist_measure=1)
    assert xv[1] == 8
    assert xe[1] == 1


def test_find_pips_stops_when_no_interior_points_remain():
    x, y = find_pips(np.array([1.0, 2.0, 3.0]), n_pips=5, dist_measure=3)
    assert x == [0, 1, 2]


def test_find_pips_rejects_bad_measure():
    with pytest.raises(ValueError):
        find_pips(np.arange(5.0), 3, dist_measure=4)
