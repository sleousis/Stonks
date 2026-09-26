"""Unit tests for ``compute_report`` metric edge cases."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from stonks.backtest.report import compute_report, periods_per_year
from stonks.core.interval import Interval


def _dates(n: int, step: timedelta) -> list[datetime]:
    start = datetime(2026, 4, 1, 14, 30, tzinfo=UTC)
    return [start + i * step for i in range(n)]


_CURVE = [100.0, 101.0, 100.5, 102.0, 101.0, 103.0]


def test_sharpe_defaults_to_daily_252_annualization():
    dates = _dates(len(_CURVE), timedelta(days=1))
    default = compute_report("s", dates, _CURVE)
    explicit = compute_report("s", dates, _CURVE, periods_per_year=252)
    assert default.sharpe == pytest.approx(explicit.sharpe)


def test_sharpe_scales_with_sqrt_of_periods_per_year():
    dates = _dates(len(_CURVE), timedelta(minutes=5))
    daily = compute_report("s", dates, _CURVE, periods_per_year=252)
    five_min = compute_report("s", dates, _CURVE, periods_per_year=252 * 78)
    assert five_min.sharpe == pytest.approx(daily.sharpe * math.sqrt(78))


def test_periods_per_year_daily_is_252():
    assert periods_per_year(Interval.DAY_1) == 252


def test_periods_per_year_intraday_uses_equity_session():
    # 6.5-hour US regular session → 78 five-minute bars, 6.5 hourly bars/day
    assert periods_per_year(Interval.MIN_5) == pytest.approx(252 * 78)
    assert periods_per_year(Interval.HOUR_1) == pytest.approx(252 * 6.5)
    # bars longer than the session still count as one bar per trading day
    assert periods_per_year(Interval.HOUR_12) == pytest.approx(252)


def test_periods_per_year_weekly_and_longer():
    assert periods_per_year(Interval.WEEK_1) == pytest.approx(52)
    assert periods_per_year(Interval.MONTH_1) == pytest.approx(12)
    assert periods_per_year(Interval.YEAR_1) == pytest.approx(1)


def test_cagr_on_tiny_intraday_span_does_not_overflow():
    dates = _dates(2, timedelta(minutes=5))
    report = compute_report("s", dates, [100.0, 101.0])
    assert report.cagr == math.inf


def test_cagr_on_total_wipeout_is_minus_one():
    dates = _dates(3, timedelta(days=30))
    report = compute_report("s", dates, [100.0, 50.0, 0.0])
    assert report.cagr == -1.0


def test_cagr_wipeout_ranks_below_flat():
    dates = _dates(3, timedelta(days=30))
    wiped = compute_report("s", dates, [100.0, 50.0, 0.0])
    flat = compute_report("s", dates, [100.0, 100.0, 100.0])
    assert wiped.cagr < flat.cagr
