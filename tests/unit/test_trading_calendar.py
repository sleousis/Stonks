"""Unit tests for asset-class-aware trading calendars (Sharpe annualization)."""

from __future__ import annotations

import pytest

from stonks.backtest.calendar import (
    ALWAYS_OPEN,
    EXCHANGE_SESSIONS,
    calendar_for,
    calendar_for_universe,
)
from stonks.backtest.report import periods_per_year
from stonks.core.interval import Interval


@pytest.mark.parametrize("asset_class", ["equity", "commodity", "bond"])
def test_exchange_traded_classes_use_the_session_calendar(asset_class):
    assert calendar_for(asset_class) is EXCHANGE_SESSIONS


def test_crypto_uses_the_always_open_calendar():
    assert calendar_for("crypto") is ALWAYS_OPEN


def test_unknown_asset_class_falls_back_to_exchange_sessions():
    assert calendar_for("forex") is EXCHANGE_SESSIONS  # type: ignore[arg-type]


def test_exchange_sessions_periods():
    assert EXCHANGE_SESSIONS.periods_per_year(Interval.DAY_1) == 252
    assert EXCHANGE_SESSIONS.periods_per_year(Interval.DAY_3) == pytest.approx(84)
    assert EXCHANGE_SESSIONS.periods_per_year(Interval.MIN_5) == pytest.approx(252 * 78)
    # whole bars per session (RS-16): 7 hourly bars, 2 four-hour bars
    assert EXCHANGE_SESSIONS.periods_per_year(Interval.HOUR_1) == pytest.approx(252 * 7)
    assert EXCHANGE_SESSIONS.periods_per_year(Interval.HOUR_4) == pytest.approx(504)
    assert EXCHANGE_SESSIONS.periods_per_year(Interval.MIN_30) == pytest.approx(252 * 13)
    # bars longer than the session still count once per session
    assert EXCHANGE_SESSIONS.periods_per_year(Interval.HOUR_12) == pytest.approx(252)


def test_always_open_periods():
    assert ALWAYS_OPEN.periods_per_year(Interval.DAY_1) == 365
    assert ALWAYS_OPEN.periods_per_year(Interval.HOUR_1) == pytest.approx(365 * 24)
    assert ALWAYS_OPEN.periods_per_year(Interval.MIN_5) == pytest.approx(365 * 288)
    assert ALWAYS_OPEN.periods_per_year(Interval.HOUR_12) == pytest.approx(365 * 2)


def test_week_and_longer_are_calendar_independent():
    for cal in (EXCHANGE_SESSIONS, ALWAYS_OPEN):
        assert cal.periods_per_year(Interval.WEEK_1) == pytest.approx(52)
        assert cal.periods_per_year(Interval.MONTH_1) == pytest.approx(12)
        assert cal.periods_per_year(Interval.MONTH_6) == pytest.approx(2)
        assert cal.periods_per_year(Interval.YEAR_1) == pytest.approx(1)


def test_universe_calendar_empty_defaults_to_exchange_sessions():
    assert calendar_for_universe([]) is EXCHANGE_SESSIONS


def test_mixed_universe_uses_the_densest_calendar():
    # The engine marks equity on the union of bar timestamps; the 24/7
    # calendar is a superset of exchange sessions, so it sets the density.
    assert calendar_for_universe(["equity", "crypto"]) is ALWAYS_OPEN
    assert calendar_for_universe(["equity", "bond"]) is EXCHANGE_SESSIONS


def test_periods_per_year_defaults_to_equity():
    assert periods_per_year(Interval.DAY_1) == 252


def test_periods_per_year_for_crypto_universe():
    assert periods_per_year(Interval.DAY_1, ["crypto"]) == 365
    assert periods_per_year(Interval.HOUR_1, ["crypto", "equity"]) == pytest.approx(365 * 24)
