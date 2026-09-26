"""Unit tests for FlagPennantStrategy (bull flags / pennants, long)."""

from __future__ import annotations

import numpy as np
import pytest

from stonks.strategies.examples.flag_pennant import FlagPennantStrategy, replay_bull_flag
from tests.nt888_synth import as_of, bars_frame, knots_path, make_lake

ORDER = 8
# pole 100 @10 -> 130 @50, a small zig-zag flag, then the breakout.
FLAG_KNOTS = [
    (0, 110.0),
    (10, 100.0),
    (50, 130.0),
    (54, 126.0),
    (57, 128.5),
    (61, 125.0),
    (75, 139.0),
    (140, 145.0),
]
# pole, then a collapse far deeper than any flag
COLLAPSE_KNOTS = [(0, 110.0), (10, 100.0), (50, 130.0), (70, 105.0), (100, 100.0)]


def _logs(closes: np.ndarray, spread: float = 0.3):
    return np.log(closes + spread), np.log(closes - spread), np.log(closes)


def _live(closes, variant, hold_mult=1.0):
    hi, lo, c = _logs(closes)
    return [
        replay_bull_flag(hi[: i + 1], lo[: i + 1], c[: i + 1], ORDER, variant, hold_mult)
        for i in range(len(c))
    ]


@pytest.fixture
def flag_lake(tmp_path):
    lake = make_lake(tmp_path, bars_frame(knots_path(FLAG_KNOTS)))
    yield lake
    lake.close()


def test_parameter_spec():
    specs = {s.name: s for s in FlagPennantStrategy.parameter_spec()}
    assert specs["variant"].bounds == ["pips", "trendline"]
    assert specs["order"].bounds == (3, 48)
    assert specs["hold_mult"].bounds == (0.5, 3.0)
    assert {"ticker", "interval", "allocation"} <= specs.keys()
    assert FlagPennantStrategy.applicable_asset_classes == ("crypto", "equity")


@pytest.mark.parametrize("variant", ["pips", "trendline"])
def test_flag_breakout_opens_a_timed_trade(variant):
    live = _live(knots_path(FLAG_KNOTS), variant)
    entries = [i for i, t in enumerate(live) if t is not None and t.entry_index == i]
    assert entries, "no breakout found"
    e = entries[0]
    assert 55 <= e <= 70
    trade = live[e]
    assert trade.tip_index == 50 and trade.base_index == 10
    assert trade.flag_width == e - 50
    assert trade.hold == int(trade.flag_width * 1.0)
    assert all(t is None for t in live[:e])
    assert all(t is not None and t.entry_index == e for t in live[e : e + trade.hold])
    assert live[e + trade.hold] is None or live[e + trade.hold].entry_index != e


def test_pips_flag_is_confirmed_by_the_resistance_break():
    closes = knots_path(FLAG_KNOTS)
    live = _live(closes, "pips")
    e = next(i for i, t in enumerate(live) if t is not None)
    # the bar before the breakout is still inside the flag
    assert closes[e] > closes[e - 1]
    assert live[e].resist_at_entry <= np.log(closes[e])


@pytest.mark.parametrize("variant", ["pips", "trendline"])
def test_no_flag_when_the_pole_collapses(variant):
    live = _live(knots_path(COLLAPSE_KNOTS), variant)
    assert all(t is None for t in live)


def test_hold_mult_scales_the_hold():
    closes = knots_path(FLAG_KNOTS)
    t1 = next(t for t in _live(closes, "pips", 1.0) if t is not None)
    t2 = next(t for t in _live(closes, "pips", 2.0) if t is not None)
    assert t2.hold == int(t1.flag_width * 2.0)


def test_estimate_return_and_features(flag_lake):
    closes = knots_path(FLAG_KNOTS)
    live = _live(closes, "pips")
    e = next(i for i, t in enumerate(live) if t is not None)
    s = FlagPennantStrategy({"order": ORDER, "ticker": "X.US", "variant": "pips"})
    assert s.estimate_return("X.US", as_of(e - 1), flag_lake) is None
    r = s.estimate_return("X.US", as_of(e), flag_lake)
    assert r is not None and r > 0
    f = s.extract_features("X.US", as_of(e), flag_lake).values
    assert f["in_trade"] == 1.0 and f["flag_width"] == e - 50
    assert s.estimate_return("OTHER.US", as_of(e), flag_lake) is None


def test_fresh_instance_matches_a_warm_one(flag_lake):
    params = {"order": ORDER, "ticker": "X.US", "variant": "trendline"}
    warm = FlagPennantStrategy(params)
    walked = [warm.estimate_return("X.US", as_of(i), flag_lake) for i in range(141)]
    fresh = [
        FlagPennantStrategy(params).estimate_return("X.US", as_of(i), flag_lake) for i in range(141)
    ]
    assert walked == fresh
    assert any(r is not None for r in walked)


@pytest.mark.parametrize("variant", ["pips", "trendline"])
def test_flag_longer_than_the_window_allows_is_skipped(variant):
    # from the pole base's rolling-window neighbourhood (bar 10 - 8) to the
    # end of the hold (64 + 14) the pattern spans 76 bars
    hi, lo, c = _logs(knots_path(FLAG_KNOTS))
    n = 65
    assert replay_bull_flag(hi[:n], lo[:n], c[:n], ORDER, variant, 1.0, window_bars=70) is None
    assert replay_bull_flag(hi[:n], lo[:n], c[:n], ORDER, variant, 1.0, window_bars=80) is not None


@pytest.mark.parametrize("variant", ["pips", "trendline"])
@pytest.mark.parametrize("window", [60, 70, 76, 90])
def test_sliding_window_never_flickers(variant, window):
    """As the replay window slides, a trade only appears on its entry bar and
    only disappears when its hold ends."""
    # 40-bar lead-in so the entry happens after the window starts sliding
    hi, lo, c = _logs(knots_path([(0, 120.0)] + [(k + 40, p) for k, p in FLAG_KNOTS]))
    prev = None
    traded = False
    for t in range(window - 1, len(c)):
        sl = slice(t - window + 1, t + 1)
        slid = replay_bull_flag(hi[sl], lo[sl], c[sl], ORDER, variant, 1.0, window_bars=window)
        cur = None if slid is None else (slid.entry_index + t - window + 1, slid)
        if t > window - 1 and cur is not None and (prev is None or prev[0] != cur[0]):
            assert cur[0] == t, f"trade appeared mid-life at {t}"
        if prev is not None and (cur is None or cur[0] != prev[0]):
            assert t - prev[0] >= prev[1].hold, f"trade vanished early at {t}"
        prev = cur
        traded |= cur is not None
    assert traded == (window >= 76)
