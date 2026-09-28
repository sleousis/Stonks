"""Session lookup and session VWAP for the intraday strategies (21.3.1)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import numpy as np
import pytest

from stonks.features.intraday import session_vwap
from stonks.features.sessions import previous_regular_session, regular_session


def test_regular_session_of_a_us_ticker_is_the_nyse_session_in_naive_utc():
    s = regular_session("A.US", datetime(2024, 3, 5, 15, 0))
    assert s is not None
    assert s.day == date(2024, 3, 5)
    # winter: 09:30 to 16:00 New York is 14:30 to 21:00 UTC
    assert s.open == datetime(2024, 3, 5, 14, 30)
    assert s.close == datetime(2024, 3, 5, 21, 0)
    assert s.open.tzinfo is None


def test_regular_session_follows_daylight_saving():
    s = regular_session("A.US", datetime(2024, 7, 9, 14, 0))
    assert s is not None
    assert s.open == datetime(2024, 7, 9, 13, 30)


def test_aware_times_are_read_as_utc():
    s = regular_session("A.US", datetime(2024, 3, 5, 15, 0, tzinfo=UTC))
    assert s is not None and s.day == date(2024, 3, 5)


def test_no_session_outside_regular_hours_on_weekends_and_holidays():
    assert regular_session("A.US", datetime(2024, 3, 5, 13, 0)) is None  # pre-market
    assert regular_session("A.US", datetime(2024, 3, 5, 21, 0)) is None  # the close itself
    assert regular_session("A.US", datetime(2024, 3, 9, 15, 0)) is None  # Saturday
    assert regular_session("A.US", datetime(2024, 12, 25, 15, 0)) is None  # Christmas


def test_early_close_moves_the_close():
    s = regular_session("A.US", datetime(2024, 11, 29, 15, 0))  # day after Thanksgiving
    assert s is not None
    assert s.close == datetime(2024, 11, 29, 18, 0)


def test_unknown_calendar_has_no_session():
    assert regular_session("NOSUFFIX", datetime(2024, 3, 5, 15, 0)) is None
    assert regular_session("A.NOWHERE", datetime(2024, 3, 5, 15, 0)) is None


def test_crypto_session_is_the_utc_day():
    s = regular_session("BTC-USD.CC", datetime(2024, 3, 9, 3, 0))
    assert s is not None
    assert s.open == datetime(2024, 3, 9) and s.close == datetime(2024, 3, 10)


def test_previous_regular_session_skips_the_weekend():
    monday = regular_session("A.US", datetime(2024, 3, 11, 15, 0))
    assert monday is not None
    prev = previous_regular_session("A.US", monday)
    assert prev is not None and prev.day == date(2024, 3, 8)


def test_session_vwap_is_the_running_volume_weighted_typical_price():
    high = np.array([11.0, 12.0])
    low = np.array([9.0, 10.0])
    close = np.array([10.0, 11.0])
    volume = np.array([100.0, 300.0])
    vwap = session_vwap(high, low, close, volume)
    assert vwap[0] == pytest.approx(10.0)
    assert vwap[1] == pytest.approx((10.0 * 100 + 11.0 * 300) / 400)


def test_session_vwap_without_volume_is_the_running_mean():
    high = low = close = np.array([10.0, 12.0, 14.0])
    vwap = session_vwap(high, low, close, np.zeros(3))
    assert list(vwap) == pytest.approx([10.0, 11.0, 12.0])


def test_session_vwap_mixes_a_zero_volume_start_with_later_volume():
    close = np.array([10.0, 20.0])
    vwap = session_vwap(close, close, close, np.array([0.0, 50.0]))
    assert vwap[0] == pytest.approx(10.0)
    assert vwap[1] == pytest.approx(20.0)


def test_session_vwap_of_nothing_is_empty():
    assert session_vwap(np.array([]), np.array([]), np.array([]), np.array([])).size == 0
