"""Which daily bar a scheduled tick must see before it buys a ticker (TO-10):
the latest session of the ticker's market that closed by the fire time."""

from __future__ import annotations

from datetime import UTC, date, datetime

from stonks.scheduling.calendar import bars_due, last_closed_session


def test_an_equity_after_the_close_needs_that_days_bar():
    at = datetime(2026, 9, 25, 20, 45, tzinfo=UTC)  # Friday, close + 45 (EDT)
    assert last_closed_session("AAPL.US", None, at) == date(2026, 9, 25)


def test_an_equity_before_the_close_needs_the_previous_sessions_bar():
    at = datetime(2026, 9, 25, 15, 0, tzinfo=UTC)
    assert last_closed_session("AAPL.US", None, at) == date(2026, 9, 24)


def test_crypto_just_after_midnight_needs_yesterdays_bar():
    at = datetime(2026, 9, 26, 0, 30, tzinfo=UTC)
    assert last_closed_session("BTC-USD.CC", "crypto", at) == date(2026, 9, 25)


def test_an_early_close_counts_from_its_own_close():
    # the day after Thanksgiving closes at 13:00 New York (18:00 UTC)
    at = datetime(2026, 11, 27, 18, 45, tzinfo=UTC)
    assert last_closed_session("AAPL.US", None, at) == date(2026, 11, 27)


def test_after_a_holiday_the_previous_session_is_due():
    # Thanksgiving Thursday is closed: Wednesday's bar is the latest due
    at = datetime(2026, 11, 26, 22, 0, tzinfo=UTC)
    assert last_closed_session("AAPL.US", None, at) == date(2026, 11, 25)


def test_a_ticker_without_a_known_calendar_has_no_requirement():
    at = datetime(2026, 9, 25, 20, 45, tzinfo=UTC)
    assert last_closed_session("NOSUFFIX", None, at) is None
    assert bars_due(["AAPL.US", "NOSUFFIX"], {}, at) == {"AAPL.US": date(2026, 9, 25)}
