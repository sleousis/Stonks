"""Unit tests for HeadShouldersStrategy (inverse head & shoulders, long)."""

from __future__ import annotations

import numpy as np
import pytest

from stonks.strategies.examples.head_shoulders import (
    HeadShouldersStrategy,
    replay_inverse_hs,
)
from tests.nt888_synth import as_of, bars_frame, knots_path, make_lake

# Lead-in so the whole pattern (and its pre-left-shoulder start) sits far
# enough from the first bar to stay in a sliding replay window for the full
# max hold.
S = 30
# left shoulder 100 @S+20, left armpit 115 @S+30, head 90 @S+45, right armpit
# 116 @S+60, right shoulder 101 @S+70, then a rally through the neckline.
PATTERN = [
    (0, 130.0),
    (20, 100.0),
    (30, 115.0),
    (45, 90.0),
    (60, 116.0),
    (70, 101.0),
]
IHS_KNOTS = [(0, 145.0)] + [(k + S, p) for k, p in PATTERN] + [(S + 90, 135.0), (S + 160, 142.0)]
N = S + 161
ORDER = 3


def _knots(tail: list[tuple[int, float]]) -> list[tuple[int, float]]:
    """The lead-in + pattern, followed by ``tail`` (unshifted bar numbers)."""
    return [(0, 145.0)] + [(k + S, p) for k, p in PATTERN + tail]


def _ihs_closes() -> np.ndarray:
    return knots_path(IHS_KNOTS)


def _neck(i: int) -> float:
    la, ra = np.log(115.0), np.log(116.0)
    return la + (i - S - 30) * (ra - la) / 30


def _first_break() -> int:
    logc = np.log(_ihs_closes())
    return next(i for i in range(S + 71, len(logc)) if logc[i] > _neck(i))


@pytest.fixture
def ihs_lake(tmp_path):
    lake = make_lake(tmp_path, bars_frame(_ihs_closes()))
    yield lake
    lake.close()


def _strategy(**overrides):
    params = {"order": ORDER, "ticker": "X.US", "interval": "1d", **overrides}
    return HeadShouldersStrategy(params)


def test_parameter_spec():
    specs = {s.name: s for s in HeadShouldersStrategy.parameter_spec()}
    assert specs["order"].bounds == (2, 48) and specs["order"].default == 6
    assert specs["early_find"].kind == "bool"
    assert specs["hold_mult"].bounds == (0.5, 3.0)
    assert {"ticker", "interval", "allocation"} <= specs.keys()
    assert not specs["ticker"].tunable
    assert HeadShouldersStrategy.applicable_asset_classes == ("crypto", "equity")


def test_replay_finds_pattern_on_neckline_break():
    logc = np.log(_ihs_closes())
    brk = _first_break()
    assert replay_inverse_hs(logc[:brk], ORDER, False, 1.0) is None
    trade = replay_inverse_hs(logc[: brk + 1], ORDER, False, 1.0)
    assert trade is not None
    assert trade.entry_index == brk
    assert trade.stop == pytest.approx(np.log(101.0))
    head_height = _neck(S + 45) - np.log(90.0)
    assert trade.target == pytest.approx(_neck(brk) + head_height)
    assert trade.max_hold == 30  # head width (60 - 30) * hold_mult


def test_estimate_return_live_only_during_the_trade(ihs_lake):
    brk = _first_break()
    s = _strategy()
    assert s.estimate_return("X.US", as_of(brk - 1), ihs_lake) is None
    r = s.estimate_return("X.US", as_of(brk), ihs_lake)
    target = _neck(brk) + (_neck(S + 45) - np.log(90.0))
    close = _ihs_closes()[brk]
    assert r == pytest.approx((np.exp(target) - close) / close)
    assert s.estimate_return("X.US", as_of(brk + 29), ihs_lake) is not None
    # max hold reached: flat again
    assert s.estimate_return("X.US", as_of(brk + 30), ihs_lake) is None


def test_early_find_enters_before_the_neckline_break(ihs_lake):
    brk = _first_break()
    s = _strategy(early_find=True)
    hits = [i for i in range(S + 71, brk + 1) if s.estimate_return("X.US", as_of(i), ihs_lake)]
    assert hits and hits[0] < brk


def test_target_hit_closes_the_trade(tmp_path):
    closes = knots_path(_knots([(90, 135.0), (110, 175.0), (160, 180.0)]))
    logc = np.log(closes)
    trade = replay_inverse_hs(logc[: S + 95], ORDER, False, 3.0)
    assert trade is not None
    hit = next(i for i in range(trade.entry_index, len(logc)) if logc[i] >= trade.target)
    assert replay_inverse_hs(logc[:hit], ORDER, False, 3.0) is not None
    assert replay_inverse_hs(logc[: hit + 1], ORDER, False, 3.0) is None


def test_stop_below_right_shoulder_closes_the_trade():
    logc = np.log(knots_path(_knots([(84, 125.0), (95, 95.0), (160, 96.0)])))
    brk = next(i for i in range(S + 71, S + 95) if logc[i] > _neck(i))
    assert replay_inverse_hs(logc[: brk + 1], ORDER, False, 3.0) is not None
    stop_bar = next(i for i in range(brk, len(logc)) if logc[i] <= np.log(101.0))
    assert replay_inverse_hs(logc[:stop_bar], ORDER, False, 3.0) is not None
    assert replay_inverse_hs(logc[: stop_bar + 1], ORDER, False, 3.0) is None


def test_no_pattern_in_a_steady_trend(tmp_path):
    rng = np.random.default_rng(0)
    closes = np.linspace(100.0, 160.0, 200) + rng.normal(0.0, 0.05, 200)
    lake = make_lake(tmp_path, bars_frame(closes))
    s = _strategy()
    assert all(s.estimate_return("X.US", as_of(i), lake) is None for i in range(200))
    lake.close()


def test_head_not_lowest_is_rejected():
    knots = list(IHS_KNOTS)
    knots[4] = (S + 45, 102.0)  # "head" above the left shoulder
    logc = np.log(knots_path(knots))
    assert all(replay_inverse_hs(logc[: i + 1], ORDER, False, 1.0) is None for i in range(N))


def test_fresh_instance_matches_a_warm_one(ihs_lake):
    warm = _strategy()
    walked = [warm.estimate_return("X.US", as_of(i), ihs_lake) for i in range(N)]
    fresh = [_strategy().estimate_return("X.US", as_of(i), ihs_lake) for i in range(N)]
    assert walked == fresh
    assert any(r is not None for r in walked)


def test_other_ticker_and_missing_history(ihs_lake):
    s = _strategy()
    assert s.estimate_return("OTHER.US", as_of(_first_break()), ihs_lake) is None
    assert s.estimate_return("X.US", as_of(3), ihs_lake) is None
    assert s.extract_features("X.US", as_of(3), ihs_lake).values == {}


def test_extract_features_describe_the_live_trade(ihs_lake):
    brk = _first_break()
    f = _strategy().extract_features("X.US", as_of(brk + 2), ihs_lake).values
    assert f["in_trade"] == 1.0
    assert f["bars_in_trade"] == 2.0
    assert f["target"] > f["close"] > f["stop"]


def test_pattern_longer_than_the_window_allows_is_skipped():
    # bars from the pattern start (S+10) to the end of its max hold
    # (entry + 30) span more than 90 bars
    logc = np.log(_ihs_closes())
    brk = _first_break()
    assert replay_inverse_hs(logc[: brk + 1], ORDER, False, 1.0, window_bars=90) is None
    assert replay_inverse_hs(logc[: brk + 1], ORDER, False, 1.0, window_bars=120) is not None


@pytest.mark.parametrize("window", [60, 90, 100, 120])
def test_sliding_window_never_flickers(window):
    """As the replay window slides, a trade only appears on its entry bar and
    only disappears on a rule exit (max hold, stop or target)."""
    logc = np.log(_ihs_closes())
    prev = None  # (absolute entry, trade)
    traded = False
    for t in range(window - 1, len(logc)):
        slid = replay_inverse_hs(
            logc[t - window + 1 : t + 1], ORDER, False, 1.0, window_bars=window
        )
        cur = None if slid is None else (slid.entry_index + t - window + 1, slid)
        first = t == window - 1
        if not first and cur is not None and (prev is None or prev[0] != cur[0]):
            assert cur[0] == t, f"trade appeared mid-life at {t}"
        if prev is not None and (cur is None or cur[0] != prev[0]):
            entry, trade = prev
            assert (
                t - entry >= trade.max_hold or logc[t] >= trade.target or logc[t] <= trade.stop
            ), f"trade vanished without a rule exit at {t}"
        prev = cur
        traded |= cur is not None
    # the pattern spans 100 bars from its start to the end of its max hold
    assert traded == (window >= 100)
