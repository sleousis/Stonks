"""Unit tests for MarketStructureBreakStrategy (hierarchical-extreme breakout)."""

from __future__ import annotations

import numpy as np
import pytest

from stonks.features.extremes import atr_directional_change
from stonks.strategies.examples.market_structure_break import (
    MarketStructureBreakStrategy,
    market_structure_positions,
)
from tests.nt888_synth import as_of, bars_frame, knots_path, make_lake

ATR_LB = 5
SPREAD = 0.3
# stair-step uptrend, then a crash through the last swing low
STAIRS = [
    (0, 100.0),
    (10, 110.0),
    (16, 104.0),
    (26, 116.0),
    (32, 108.0),
    (42, 122.0),
    (48, 114.0),
    (56, 124.0),
    (70, 95.0),
    (80, 96.0),
]


def _arrays(knots=STAIRS):
    c = knots_path(knots)
    return c + SPREAD, c - SPREAD, c


def test_parameter_spec():
    specs = {s.name: s for s in MarketStructureBreakStrategy.parameter_spec()}
    assert specs["atr_lookback"].bounds == (14, 500)
    assert specs["level"].bounds == (0, 4)
    assert {"ticker", "interval", "allocation"} <= specs.keys()
    assert MarketStructureBreakStrategy.applicable_asset_classes == ("crypto", "equity")


def test_level0_long_above_last_high_flat_below_last_low():
    h, lo, c = _arrays()
    pos = market_structure_positions(h, lo, c, ATR_LB, level=0)
    ext = atr_directional_change(h, lo, c, ATR_LB)

    def last(kind, i):
        seen = [e for e in ext if e.kind == kind and e.conf_index <= i]
        return seen[-1].price if seen else np.nan

    expected = np.zeros(len(c))
    state = 0.0
    for i in range(len(c)):
        if c[i] > last("top", i):
            state = 1.0
        elif c[i] < last("bottom", i):
            state = 0.0
        expected[i] = state
    np.testing.assert_array_equal(pos, expected)
    # the stairs produce a long, and the crash flattens it
    assert pos.max() == 1.0
    assert pos[-1] == 0.0
    first_long = int(np.argmax(pos))
    assert 16 < first_long <= 26


def test_positions_are_causal():
    rng = np.random.default_rng(5)
    c = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.01, 700)))
    h, lo = c * 1.004, c * 0.996
    full = market_structure_positions(h, lo, c, 14, level=1)
    pre = market_structure_positions(h[:400], lo[:400], c[:400], 14, level=1)
    np.testing.assert_array_equal(pre, full[:400])
    assert full.max() == 1.0


def test_missing_level_means_flat():
    h, lo, c = _arrays()
    # level 4 needs far more swings than this short series has
    assert not market_structure_positions(h, lo, c, ATR_LB, level=4).any()


@pytest.fixture
def stairs_lake(tmp_path):
    lake = make_lake(tmp_path, bars_frame(knots_path(STAIRS), spread=SPREAD))
    yield lake
    lake.close()


def _strategy(**kw):
    # lowest allowed ATR lookback, so the short series has enough swings
    return MarketStructureBreakStrategy(
        {"atr_lookback": 14, "level": 0, "ticker": "X.US", "interval": "1d", **kw}
    )


def test_estimate_return_tracks_the_position(stairs_lake):
    h, lo, c = _arrays()
    pos = market_structure_positions(h, lo, c, 14, level=0)
    s = _strategy()
    got = [s.estimate_return("X.US", as_of(i), stairs_lake) for i in range(len(c))]
    assert [r is not None for r in got] == [bool(p) for p in pos]
    assert any(r is not None and r > 0 for r in got)
    assert s.estimate_return("OTHER.US", as_of(40), stairs_lake) is None


def test_fresh_instance_matches_a_warm_one(stairs_lake):
    warm = _strategy()
    walked = [warm.estimate_return("X.US", as_of(i), stairs_lake) for i in range(81)]
    fresh = [_strategy().estimate_return("X.US", as_of(i), stairs_lake) for i in range(81)]
    assert walked == fresh


def test_extract_features(stairs_lake):
    f = _strategy().extract_features("X.US", as_of(60), stairs_lake).values
    assert {"close", "level_high", "level_low", "position"} <= f.keys()
