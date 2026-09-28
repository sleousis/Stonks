"""Volatility-scaled trailing stops and the stateless trade replay (BL-40)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from stonks.features.trailing_stop import Trade, replay_trades, stop_exit, trailing_stop_levels

ONES = np.ones(8)


def test_levels_by_hand():
    closes = np.array([10.0, 11.0, 12.0, 11.5, 9.0])
    levels = trailing_stop_levels(closes, np.ones(5), k=2.0)
    assert levels.tolist() == [8.0, 9.0, 10.0, 10.0, 10.0]


def test_levels_never_drop_when_atr_widens_or_price_falls():
    closes = np.array([10.0, 12.0, 11.0, 13.0, 9.0, 8.0])
    atrs = np.array([0.5, 0.5, 2.0, 3.0, 5.0, 9.0])
    levels = trailing_stop_levels(closes, atrs, k=2.0)
    assert (np.diff(levels) >= 0).all()
    assert levels[-1] == 11.0  # 12 - 2 * 0.5, set on bar 1


def test_levels_wait_for_a_valid_atr():
    levels = trailing_stop_levels(np.array([10.0, 11.0, 12.0]), np.array([np.nan, np.nan, 1.0]), 3)
    assert math.isnan(levels[0]) and math.isnan(levels[1])
    assert levels[2] == 9.0


def test_stop_exit_by_hand():
    closes = np.array([10.0, 11.0, 12.0, 11.5, 9.0, 13.0])
    assert stop_exit(closes, np.ones(6), entry=0, k=2.0) == 4


def test_stop_exit_uses_the_level_set_on_the_previous_bar():
    # a shrinking ATR on bar 2 raises that bar's level to 19, but the stop in
    # force during bar 2 is the 16 set at bar 1's close: no exit on bar 2
    closes = np.array([10.0, 20.0, 17.5, 15.0])
    atrs = np.array([1.0, 2.0, 0.5, 0.5])
    assert stop_exit(closes, atrs, entry=0, k=2.0) == 3


def test_stop_exit_never_triggers_on_the_entry_bar_and_may_not_trigger():
    closes = np.array([10.0, 10.5, 11.0])
    assert stop_exit(closes, ONES[:3], entry=0, k=2.0) is None


def test_stop_exit_is_causal():
    closes = np.array([10.0, 11.0, 12.0, 11.5, 9.0, 13.0])
    changed = closes.copy()
    changed[5] = 1.0
    assert stop_exit(closes, np.ones(6), 0, 2.0) == stop_exit(changed, np.ones(6), 0, 2.0)
    assert stop_exit(closes[:4], np.ones(4), 0, 2.0) is None


# ---- replay -----------------------------------------------------------------------


def test_replay_enters_on_the_first_entry_bar_and_holds_without_a_stop():
    closes = np.arange(10.0, 18.0)
    entries = np.array([False, False, True, True, False, False, False, False])
    assert replay_trades(closes, ONES, entries, k=3.0) == [Trade(2, None, None)]


def test_replay_exits_on_the_stop_and_reenters_on_a_later_entry():
    closes = np.array([10.0, 12.0, 9.0, 9.5, 13.0, 14.0, 15.0, 16.0])
    entries = np.array([True, False, False, False, True, False, False, False])
    trades = replay_trades(closes, ONES, entries, k=2.0)
    assert trades == [Trade(0, 2, "stop"), Trade(4, None, None)]


def test_replay_exits_when_the_hold_signal_turns_off():
    closes = np.arange(10.0, 18.0)
    on = np.array([False, True, True, True, False, True, True, True])
    trades = replay_trades(closes, ONES, on, k=3.0, holds=on)
    assert trades == [Trade(1, 4, "signal"), Trade(5, None, None)]


def test_replay_cooldown_blocks_reentry_after_a_stop_only():
    closes = np.array([10.0, 12.0, 9.0, 10.0, 11.0, 12.0, 13.0, 14.0])
    on = np.ones(8, dtype=bool)
    trades = replay_trades(closes, ONES, on, k=2.0, holds=on, cooldown_bars=3)
    # stopped on bar 2; bars 3..5 cool down; back in on bar 6
    assert trades == [Trade(0, 2, "stop"), Trade(6, None, None)]
    no_cooldown = replay_trades(closes, ONES, on, k=2.0, holds=on)
    assert no_cooldown[1] == Trade(3, None, None)


def test_replay_rejects_mismatched_lengths_and_bad_k():
    with pytest.raises(ValueError):
        replay_trades(np.ones(3), np.ones(2), np.ones(3, dtype=bool), k=2.0)
    with pytest.raises(ValueError):
        replay_trades(np.ones(3), np.ones(3), np.ones(3, dtype=bool), k=0.0)
    with pytest.raises(ValueError):
        replay_trades(np.ones(3), np.ones(3), np.ones(3, dtype=bool), k=1.0, cooldown_bars=-1)


def test_replay_prefix_is_stable_when_bars_are_appended():
    rng = np.random.default_rng(1)
    closes = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, 300)))
    atrs = np.full(300, 2.0)
    entries = rng.random(300) < 0.05
    full = replay_trades(closes, atrs, entries, k=2.0)
    part = replay_trades(closes[:150], atrs[:150], entries[:150], k=2.0)
    closed = [t for t in full if t.exit is not None and t.exit < 150]
    assert [t for t in part if t.exit is not None] == closed


def test_a_missing_close_does_not_freeze_the_high_water_mark():
    """One NaN close spread through ``maximum.accumulate`` and the stop
    never rose again: [10, nan, 12, 14, 13] with ATR 1 stayed at 9."""
    levels = trailing_stop_levels(np.array([10.0, np.nan, 12.0, 14.0, 13.0]), np.ones(5), 1.0)
    assert levels.tolist() == pytest.approx([9.0, 9.0, 11.0, 13.0, 13.0])
