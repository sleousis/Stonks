"""Fundamentals helpers in features/library: TTM from quarterly filings."""

from __future__ import annotations

import math
from datetime import date

from stonks.features.library import ttm_from_quarters

Q = [date(2023, 3, 31), date(2023, 6, 30), date(2023, 9, 30), date(2023, 12, 31)]


def test_sums_four_consecutive_quarters():
    assert ttm_from_quarters(Q, [1.0, 2.0, 3.0, 4.0]) == 10.0


def test_uses_only_the_last_four():
    periods = [date(2022, 12, 31), *Q]
    assert ttm_from_quarters(periods, [100.0, 1.0, 2.0, 3.0, 4.0]) == 10.0


def test_fewer_than_four_is_none():
    assert ttm_from_quarters(Q[:3], [1.0, 2.0, 3.0]) is None
    assert ttm_from_quarters([], []) is None


def test_missing_value_is_none():
    assert ttm_from_quarters(Q, [1.0, float("nan"), 3.0, 4.0]) is None
    assert ttm_from_quarters(Q, [1.0, None, 3.0, 4.0]) is None


def test_gap_in_quarters_is_none():
    gapped = [date(2022, 12, 31), date(2023, 6, 30), date(2023, 9, 30), date(2023, 12, 31)]
    assert ttm_from_quarters(gapped, [1.0, 2.0, 3.0, 4.0]) is None


def test_52_53_week_fiscal_quarters_are_accepted():
    fiscal = [date(2023, 4, 1), date(2023, 7, 1), date(2023, 9, 30), date(2023, 12, 30)]
    assert ttm_from_quarters(fiscal, [1.0, 1.0, 1.0, 1.0]) == 4.0


def test_negative_values_sum_normally():
    out = ttm_from_quarters(Q, [-1.0, -2.0, 3.0, -4.0])
    assert out is not None and math.isclose(out, -4.0)
