"""Unit tests for HarmonicXABCDStrategy (bullish XABCD patterns, long)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from stonks.features.extremes import directional_change
from stonks.strategies.examples.harmonic_xabcd import (
    TEMPLATES,
    HarmonicXABCDStrategy,
    ratio_error,
    replay_bull_harmonic,
)
from tests.nt888_synth import as_of, bars_frame, knots_path, make_lake

SIGMA = 0.03
SPREAD = 0.3
# bullish Gartley: X 100 @15, A 130 @35, B 111.46 @50 (0.618 of XA),
# C 122.92 @62 (0.618 of AB), D 106.42 @80 (0.786 of XA), then a rally.
GARTLEY_KNOTS = [
    (0, 120.0),
    (15, 100.0),
    (35, 130.0),
    (50, 111.46),
    (62, 122.92),
    (80, 106.42),
    (100, 118.0),
]


def _arrays(knots):
    c = knots_path(knots)
    return c + SPREAD, c - SPREAD, c


def _live(knots, err_thresh=0.2):
    h, lo, c = _arrays(knots)
    return [
        replay_bull_harmonic(h[: i + 1], lo[: i + 1], c[: i + 1], SIGMA, err_thresh)
        for i in range(len(c))
    ]


def test_templates_match_the_original_ratios():
    t = {tpl.name: tpl for tpl in TEMPLATES}
    assert set(t) == {"Gartley", "Bat", "Butterfly", "Crab", "Deep Crab", "Cypher", "Shark"}
    assert t["Gartley"].ab_xa == 0.618 and t["Gartley"].ad_xa == 0.786
    assert t["Gartley"].bc_ab == (0.382, 0.886) and t["Gartley"].cd_bc == (1.13, 1.618)
    assert t["Bat"].ab_xa == (0.382, 0.50) and t["Bat"].cd_bc == (1.618, 2.618)
    assert t["Butterfly"].ad_xa == (1.27, 1.41)
    assert t["Crab"].cd_bc == (2.618, 3.618) and t["Crab"].ad_xa == 1.618
    assert t["Deep Crab"].ab_xa == 0.886 and t["Deep Crab"].cd_bc == (2.0, 3.618)
    assert t["Cypher"].bc_ab == (1.13, 1.41) and t["Cypher"].cd_bc == (1.27, 2.00)
    assert t["Shark"].ab_xa is None and t["Shark"].ad_xa == (0.886, 1.13)


def test_ratio_error():
    assert ratio_error(0.5, None) == 0.0
    assert ratio_error(0.618, 0.618) == 0.0
    assert ratio_error(0.5, 0.618) == pytest.approx(abs(math.log(0.5 / 0.618)))
    assert ratio_error(0.5, (0.382, 0.886)) == 0.0
    # outside a range: twice the log distance to the nearest edge
    assert ratio_error(1.0, (0.382, 0.886)) == pytest.approx(2 * math.log(1.0 / 0.886))


def test_parameter_spec():
    specs = {s.name: s for s in HarmonicXABCDStrategy.parameter_spec()}
    assert specs["sigma"].bounds == (0.005, 0.06)
    assert specs["err_thresh"].bounds == (0.1, 0.75)
    assert {"ticker", "interval", "allocation"} <= specs.keys()
    assert HarmonicXABCDStrategy.applicable_asset_classes == ("crypto", "equity")


def test_gartley_entered_on_the_cd_leg_and_held_until_next_extreme():
    h, lo, c = _arrays(GARTLEY_KNOTS)
    ext = directional_change(h, lo, c, SIGMA)
    c_top = next(e for e in ext if e.ext_index == 62)
    d_bottom = next(e for e in ext if e.ext_index == 80)
    live = _live(GARTLEY_KNOTS)
    entry = next(i for i, t in enumerate(live) if t is not None)
    trade = live[entry]
    assert c_top.conf_index <= entry <= 80
    assert trade.name == "Gartley"
    assert (trade.x_index, trade.a_index, trade.b_index, trade.c_index) == (15, 35, 50, 62)
    assert trade.d_price == pytest.approx(lo[entry])
    assert trade.error <= 0.2
    assert all(t is not None and t.entry_index == entry for t in live[entry : d_bottom.conf_index])
    assert all(t is None for t in live[d_bottom.conf_index :])


def test_entry_never_uses_an_unconfirmed_c():
    # before C (the last top) is confirmed the CD leg does not exist yet
    h, lo, c = _arrays(GARTLEY_KNOTS)
    c_top = next(e for e in directional_change(h, lo, c, SIGMA) if e.ext_index == 62)
    live = _live(GARTLEY_KNOTS)
    assert all(t is None for t in live[: c_top.conf_index])


def test_bad_ratios_are_rejected():
    # B barely retraces XA (AB/XA ~ 0.15): no template is close
    knots = [(0, 120.0), (15, 100.0), (35, 130.0), (45, 125.5), (55, 129.0), (80, 104.0)]
    assert all(t is None for t in _live(knots))


def test_straight_decline_has_no_pattern():
    assert all(t is None for t in _live([(0, 130.0), (100, 90.0)]))


@pytest.fixture
def gartley_lake(tmp_path):
    lake = make_lake(tmp_path, bars_frame(knots_path(GARTLEY_KNOTS), spread=SPREAD))
    yield lake
    lake.close()


def test_estimate_return_and_features(gartley_lake):
    live = _live(GARTLEY_KNOTS)
    entry = next(i for i, t in enumerate(live) if t is not None)
    s = HarmonicXABCDStrategy(
        {"sigma": SIGMA, "err_thresh": 0.2, "ticker": "X.US", "interval": "1d"}
    )
    assert s.estimate_return("X.US", as_of(entry - 1), gartley_lake) is None
    r = s.estimate_return("X.US", as_of(entry), gartley_lake)
    assert r is not None and r > 0
    f = s.extract_features("X.US", as_of(entry), gartley_lake).values
    assert f["in_trade"] == 1.0
    assert f["pattern_error"] <= 0.2
    assert s.estimate_return("OTHER.US", as_of(entry), gartley_lake) is None


def test_fresh_instance_matches_a_warm_one(gartley_lake):
    params = {"sigma": SIGMA, "ticker": "X.US", "interval": "1d"}
    warm = HarmonicXABCDStrategy(params)
    walked = [warm.estimate_return("X.US", as_of(i), gartley_lake) for i in range(101)]
    fresh = [
        HarmonicXABCDStrategy(params).estimate_return("X.US", as_of(i), gartley_lake)
        for i in range(101)
    ]
    assert walked == fresh
    assert any(r is not None for r in walked)


def test_err_thresh_gates_entries():
    strict = _live(GARTLEY_KNOTS, err_thresh=0.1)
    loose = _live(GARTLEY_KNOTS, err_thresh=0.75)
    first = [next((i for i, t in enumerate(x) if t is not None), np.inf) for x in (strict, loose)]
    assert first[1] <= first[0]
