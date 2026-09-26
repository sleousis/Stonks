"""Shared plumbing of the BL-40 trend strategies: FDM, period ends, scalar
estimation."""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from stonks.strategies.examples._forecast_trend import (
    MIN_ESTIMATION_BARS,
    forecast_diversification_multiplier,
    period_end_mask,
    rule_scalar,
)

# zero-mean, mutually orthogonal +/-1 patterns (Walsh functions of length 8)
_WALSH = np.array(
    [
        [1, -1, 1, -1, 1, -1, 1, -1],
        [1, 1, -1, -1, 1, 1, -1, -1],
        [1, -1, -1, 1, 1, -1, -1, 1],
        [1, 1, 1, 1, -1, -1, -1, -1],
        [1, -1, 1, -1, -1, 1, -1, 1],
        [1, 1, -1, -1, -1, -1, 1, 1],
        [1, -1, -1, 1, -1, 1, 1, -1],
    ],
    dtype=float,
)
ROWS = 256  # a multiple of 8


def _orthogonal(k: int, rows: int = ROWS) -> pd.DataFrame:
    return pd.DataFrame({f"r{i}": np.tile(_WALSH[i], rows // 8) for i in range(k)})


# ---- FDM -----------------------------------------------------------------------------


@pytest.mark.parametrize("k", [2, 3, 5])
def test_fdm_of_uncorrelated_rules_is_sqrt_n(k):
    assert forecast_diversification_multiplier(_orthogonal(k), 1.1) == pytest.approx(math.sqrt(k))


def test_fdm_of_identical_rules_is_one():
    col = _orthogonal(1)["r0"]
    rules = pd.DataFrame({"a": col, "b": col, "c": col})
    assert forecast_diversification_multiplier(rules, 1.1) == pytest.approx(1.0)


def test_fdm_floors_negative_correlation_at_zero():
    col = _orthogonal(1)["r0"]
    rules = pd.DataFrame({"a": col, "b": -col})
    assert forecast_diversification_multiplier(rules, 1.1) == pytest.approx(math.sqrt(2))


def test_fdm_with_a_known_partial_correlation():
    # corr(a, b) = 0.5 exactly: b = 0.5 a + sqrt(0.75) c with a, c orthogonal
    o = _orthogonal(2)
    rules = pd.DataFrame({"a": o["r0"], "b": 0.5 * o["r0"] + math.sqrt(0.75) * o["r1"]})
    expected = 1.0 / math.sqrt(0.25 * (1 + 1 + 2 * 0.5))
    assert forecast_diversification_multiplier(rules, 1.1) == pytest.approx(expected)


def test_fdm_is_capped_at_two_and_a_half():
    assert forecast_diversification_multiplier(_orthogonal(7), 1.1) == 2.5


def test_fdm_falls_back_with_too_little_history_and_is_one_for_one_rule():
    short = _orthogonal(2, rows=MIN_ESTIMATION_BARS - 10)
    assert forecast_diversification_multiplier(short, 1.3) == 1.3
    assert forecast_diversification_multiplier(_orthogonal(1), 1.3) == 1.0


def test_fdm_counts_only_rows_where_every_rule_has_a_value():
    rules = _orthogonal(2)
    rules.iloc[: ROWS - MIN_ESTIMATION_BARS + 1, 0] = np.nan
    assert forecast_diversification_multiplier(rules, 1.3) == 1.3


# ---- forecast scalar ----------------------------------------------------------------------


def test_rule_scalar_fixed_mode_ignores_history():
    assert rule_scalar(pd.Series([1.0] * 400), 5.3, "fixed") == 5.3


def test_rule_scalar_estimate_mode_targets_mean_abs_ten():
    raw = pd.Series([2.0, -2.0] * 200)
    assert rule_scalar(raw, 5.3, "estimate") == pytest.approx(5.0)


def test_rule_scalar_estimate_mode_falls_back_without_a_year():
    raw = pd.Series([2.0] * (MIN_ESTIMATION_BARS - 1) + [np.nan] * 50)
    assert rule_scalar(raw, 5.3, "estimate") == 5.3


# ---- period ends ------------------------------------------------------------------------


def _days(start: str, end: str, crypto: bool = False) -> list[date]:
    rng = pd.date_range(start, end) if crypto else pd.bdate_range(start, end)
    return [d.date() for d in rng]


def test_week_end_mask_from_the_next_bar_and_the_calendar():
    days = _days("2024-06-03", "2024-06-14")  # Mon .. Fri, two weeks
    mask = period_end_mask(days, "equity", "week")
    assert [d for d, m in zip(days, mask, strict=True) if m] == [
        date(2024, 6, 7),
        date(2024, 6, 14),
    ]
    # a Thursday as the latest bar is not the week's end ...
    assert not period_end_mask(days[:-1], "equity", "week")[-1]
    # ... unless Friday is a holiday (Good Friday 2024-03-29)
    assert period_end_mask([date(2024, 3, 28)], "equity", "week")[-1]


def test_week_end_mask_for_crypto_is_sunday():
    days = _days("2024-06-03", "2024-06-16", crypto=True)
    mask = period_end_mask(days, "crypto", "week")
    assert [d for d, m in zip(days, mask, strict=True) if m] == [
        date(2024, 6, 9),
        date(2024, 6, 16),
    ]
    assert not period_end_mask(days[:-1], "crypto", "week")[-1]  # Saturday


def test_month_end_mask():
    assert period_end_mask([date(2024, 5, 31)], "equity", "month")[-1]
    assert not period_end_mask([date(2024, 5, 30)], "equity", "month")[-1]
    assert period_end_mask([date(2024, 6, 30)], "crypto", "month")[-1]
    assert not period_end_mask([date(2024, 6, 28)], "crypto", "month")[-1]
    days = _days("2024-05-27", "2024-06-05")
    mask = period_end_mask(days, "equity", "month")
    assert [d for d, m in zip(days, mask, strict=True) if m] == [date(2024, 5, 31)]


def test_period_end_mask_rejects_unknown_period():
    with pytest.raises(ValueError):
        period_end_mask([date(2024, 1, 2)], "equity", "year")
