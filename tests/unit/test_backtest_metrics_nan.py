"""A NaN in an equity curve shows up in its drawdowns instead of hiding."""

from __future__ import annotations

import math

from stonks.backtest import metrics


def test_drawdowns_propagate_nan_from_the_bad_point_on():
    dd = metrics.drawdowns([100.0, 90.0, float("nan"), 95.0])
    assert dd[0] == 0.0
    assert math.isclose(dd[1], -0.1)
    assert math.isnan(dd[2])
    assert math.isnan(dd[3])


def test_nan_first_point_is_not_a_zero_drawdown():
    dd = metrics.drawdowns([float("nan"), 100.0, 90.0])
    assert all(math.isnan(v) for v in dd)


def test_max_drawdown_of_a_curve_with_nan_is_nan():
    assert math.isnan(metrics.max_drawdown([100.0, 80.0, float("nan"), 120.0]))
    assert math.isnan(metrics.max_drawdown([float("nan"), 100.0, 90.0]))


def test_clean_curves_are_unchanged():
    assert metrics.drawdowns([100.0, 110.0, 99.0]) == [0.0, 0.0, (99.0 - 110.0) / 110.0]
    assert metrics.max_drawdown([]) == 0.0
    assert metrics.drawdowns([0.0, 0.0]) == [0.0, 0.0]
