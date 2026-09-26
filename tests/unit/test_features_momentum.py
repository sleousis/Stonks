"""Gray & Vogel momentum features: 12-2 return, information discreteness,
quarterly rebalance days."""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from stonks.features.momentum import (
    formation_window,
    information_discreteness,
    is_quarter_rebalance_day,
    trailing_return_skip,
)


def test_trailing_return_skip_by_hand():
    closes = np.arange(1.0, 301.0)  # closes[i] = i + 1
    # t = index 299; C_{t-21} = closes[278] = 279, C_{t-252} = closes[47] = 48
    assert trailing_return_skip(closes, 252, 21) == pytest.approx(279 / 48 - 1)


def test_trailing_return_skip_ignores_the_skip_month():
    closes = np.full(253, 100.0)
    closes[-21:] = 1_000.0  # a spike inside the skipped month
    assert trailing_return_skip(closes, 252, 21) == pytest.approx(0.0)


def test_trailing_return_skip_needs_formation_plus_one_closes():
    assert trailing_return_skip(np.ones(252), 252, 21) is None
    assert trailing_return_skip(np.ones(253), 252, 21) == 0.0


def test_trailing_return_skip_rejects_bad_windows_and_prices():
    with pytest.raises(ValueError):
        trailing_return_skip(np.ones(300), 21, 21)
    closes = np.ones(253)
    closes[0] = 0.0
    assert trailing_return_skip(closes, 252, 21) is None


def test_formation_window_ends_at_the_skip_boundary():
    closes = np.arange(300.0)
    window = formation_window(closes, 252, 21)
    assert window[0] == 300 - 253 and window[-1] == 300 - 22
    assert len(window) == 232


def test_information_discreteness_is_minus_one_for_all_positive_days():
    closes = 100 * 1.01 ** np.arange(50)
    assert information_discreteness(closes) == pytest.approx(-1.0)


def test_information_discreteness_is_minus_one_for_a_smooth_decline():
    closes = 100 * 0.99 ** np.arange(50)
    assert information_discreteness(closes) == pytest.approx(-1.0)


def test_information_discreteness_by_hand():
    # returns: +, +, -, +, 0 -> total up; %neg = 1/5, %pos = 3/5
    closes = np.array([10.0, 11.0, 12.0, 11.5, 12.5, 12.5])
    assert information_discreteness(closes) == pytest.approx(1 / 5 - 3 / 5)


def test_jumpy_winner_has_higher_id_than_a_smooth_one():
    rng = np.random.default_rng(0)
    smooth = 100 * np.cumprod(1 + np.full(200, 0.001))
    jumpy_rets = np.where(rng.random(200) < 0.55, -0.004, 0.0)
    jumpy_rets[::40] = 0.15
    jumpy = 100 * np.cumprod(1 + jumpy_rets)
    assert jumpy[-1] > smooth[-1]
    assert information_discreteness(jumpy) > information_discreteness(smooth)


def test_information_discreteness_needs_two_closes():
    assert information_discreteness(np.array([1.0])) is None


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2024, 2, 29), True),
        (date(2024, 2, 28), False),
        (date(2024, 3, 28), False),  # last session of March: not a rebalance month
        (date(2021, 5, 28), True),  # 31st is Memorial Day
        (date(2021, 5, 31), False),
        (date(2024, 8, 30), True),
        (date(2024, 11, 29), True),
    ],
)
def test_quarter_rebalance_day_is_the_last_session_of_the_month(day, expected):
    assert is_quarter_rebalance_day(day) is expected


def test_quarter_rebalance_months_are_configurable():
    assert is_quarter_rebalance_day(date(2024, 3, 28), months=(3, 6, 9, 12))
