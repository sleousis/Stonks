"""Market calendars (roadmap 12.1). Hermetic: exchange_calendars ships its
holiday rules with the package, no network."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from stonks.scheduling.calendar import (
    AlwaysOpenCalendar,
    CalendarRangeError,
    MarketCalendar,
    Session,
    UnknownCalendarError,
    calendar_for_exchange,
    calendar_for_ticker,
    get_calendar,
    register_calendar,
    trading_calendars,
    universe_trades_on,
)


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


# ---- exchange calendars ------------------------------------------------------


def test_nyse_regular_session_in_utc_follows_dst():
    nyse = get_calendar("XNYS")
    winter = nyse.session(date(2026, 1, 5))
    summer = nyse.session(date(2026, 7, 6))
    assert winter == Session(date(2026, 1, 5), _utc(2026, 1, 5, 14, 30), _utc(2026, 1, 5, 21))
    assert summer.open == _utc(2026, 7, 6, 13, 30)
    assert summer.close == _utc(2026, 7, 6, 20)


def test_nyse_between_us_and_eu_dst_switch():
    # US moved to EDT on 2026-03-08; the close is 20:00 UTC from the Monday.
    assert get_calendar("XNYS").session(date(2026, 3, 9)).close == _utc(2026, 3, 9, 20)
    # XETRA is still on CET that week: close 17:30 CET = 16:30 UTC.
    assert get_calendar("XETR").session(date(2026, 3, 9)).close == _utc(2026, 3, 9, 16, 30)


def test_holidays_weekends_and_early_closes():
    nyse = get_calendar("XNYS")
    assert not nyse.is_trading_day(date(2026, 11, 26))  # Thanksgiving
    assert not nyse.is_trading_day(date(2026, 9, 26))  # Saturday
    assert nyse.session(date(2026, 9, 26)) is None
    # Day after Thanksgiving closes at 13:00 ET.
    assert nyse.session(date(2026, 11, 27)).close == _utc(2026, 11, 27, 18)
    assert date(2026, 11, 26) in nyse.holidays(date(2026, 11, 1), date(2026, 11, 30))
    assert date(2026, 11, 28) not in nyse.holidays(date(2026, 11, 1), date(2026, 11, 30))


def test_sessions_range_and_neighbours():
    nyse = get_calendar("XNYS")
    days = [s.date for s in nyse.sessions(date(2026, 11, 25), date(2026, 11, 30))]
    assert days == [date(2026, 11, 25), date(2026, 11, 27), date(2026, 11, 30)]
    assert nyse.next_session(date(2026, 11, 25)).date == date(2026, 11, 27)
    assert nyse.previous_session(date(2026, 11, 27)).date == date(2026, 11, 25)


def test_next_open_and_close():
    nyse = get_calendar("XNYS")
    # Friday after the close -> Monday.
    friday_evening = _utc(2026, 9, 25, 22)
    assert nyse.next_open(friday_evening) == _utc(2026, 9, 28, 13, 30)
    assert nyse.next_close(friday_evening) == _utc(2026, 9, 28, 20)
    # Mid-session: next close is today's.
    assert nyse.next_close(_utc(2026, 9, 25, 15)) == _utc(2026, 9, 25, 20)
    assert nyse.is_open_at(_utc(2026, 9, 25, 15))
    assert not nyse.is_open_at(_utc(2026, 9, 25, 21))


def test_out_of_range_raises():
    with pytest.raises(CalendarRangeError):
        get_calendar("XNYS").session(date(2090, 1, 3))


def test_naive_datetimes_are_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        get_calendar("XNYS").next_open(datetime(2026, 9, 25, 22))


# ---- 24/7 ------------------------------------------------------------------------


def test_always_open_calendar():
    cal = get_calendar("24/7")
    assert isinstance(cal, AlwaysOpenCalendar)
    sat = cal.session(date(2026, 9, 26))
    assert sat == Session(date(2026, 9, 26), _utc(2026, 9, 26), _utc(2026, 9, 27))
    assert cal.holidays(date(2026, 1, 1), date(2026, 12, 31)) == []
    assert cal.is_open_at(_utc(2026, 12, 25, 3))
    assert cal.next_close(_utc(2026, 9, 26, 5)) == _utc(2026, 9, 27)


# ---- registry and mapping ---------------------------------------------------------


def test_exchange_codes_map_to_calendars():
    assert calendar_for_exchange("US").name == "XNYS"
    assert calendar_for_exchange("xetra").name == "XETR"
    assert calendar_for_exchange("LSE").name == "XLON"
    assert calendar_for_exchange("CC").name == "24/7"
    with pytest.raises(UnknownCalendarError):
        calendar_for_exchange("NOPE")


def test_ticker_suffix_and_asset_class():
    assert calendar_for_ticker("AAPL.US").name == "XNYS"
    assert calendar_for_ticker("VOD.LSE").name == "XLON"
    assert calendar_for_ticker("BTC-USD.CC").name == "24/7"
    # asset class wins over the suffix
    assert calendar_for_ticker("BTC-USD.US", asset_class="crypto").name == "24/7"
    with pytest.raises(UnknownCalendarError):
        calendar_for_ticker("AAPL")


def _exchange_codes() -> list[str]:
    from stonks.scheduling.calendar import EXCHANGE_CALENDARS

    return sorted(EXCHANGE_CALENDARS)


# One case per exchange, so the workers share them (BE-68, TT-17).
@pytest.mark.slow
@pytest.mark.parametrize("code", _exchange_codes())
def test_every_mapped_exchange_resolves(code):
    assert isinstance(calendar_for_exchange(code), MarketCalendar)


def test_register_custom_calendar():
    class Never(MarketCalendar):
        name = "NEVER"

        def session(self, day: date) -> Session | None:
            return None

    register_calendar("NEVER", Never)
    assert get_calendar("NEVER").is_trading_day(date(2026, 1, 5)) is False
    with pytest.raises(UnknownCalendarError):
        get_calendar("NOT-A-CALENDAR")


# ---- universes -------------------------------------------------------------------


def test_trading_calendars_dedupes_and_reports_unknown():
    cals, unknown = trading_calendars(["AAPL.US", "MSFT.US", "X", "VOD.LSE"], {})
    assert sorted(c.name for c in cals) == ["XLON", "XNYS"]
    assert unknown == ["X"]


def test_equity_universe_skips_holiday_but_crypto_trades():
    thanksgiving = date(2026, 11, 26)
    assert not universe_trades_on(["AAPL.US", "MSFT.US"], thanksgiving, {})
    assert universe_trades_on(["AAPL.US", "BTC-USD.CC"], thanksgiving, {})
    assert universe_trades_on(["AAPL.US", "ETH"], thanksgiving, {"ETH": "crypto"})
    # London is open on US Thanksgiving.
    assert universe_trades_on(["AAPL.US", "VOD.LSE"], thanksgiving, {})


def test_unknown_calendar_never_skips():
    # An instrument we can't place on a calendar must not silently stop trading.
    assert universe_trades_on(["MYSTERY"], date(2026, 9, 26), {})
    assert universe_trades_on([], date(2026, 9, 26), {})
