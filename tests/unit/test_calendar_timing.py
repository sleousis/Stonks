"""Does an earnings report fall before the next market open? The order
ticket warns when it does (roadmap 20.7)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from stonks.calendars.models import EarningsEvent
from stonks.calendars.timing import earnings_before_next_open
from stonks.scheduling.calendar import get_calendar

NYSE = get_calendar("XNYS")


def _event(ticker: str, day: date, timing=None) -> EarningsEvent:
    return EarningsEvent(
        ticker=ticker, period_end=date(2026, 9, 30), report_date=day, before_after_market=timing
    )


def _flagged(events, now):
    return [
        w.ticker for w in earnings_before_next_open(events, now=now, calendar_for=lambda t: NYSE)
    ]


# Thursday 2026-10-29. NYSE opens 13:30 UTC and closes 20:00 UTC (EDT).
THU_MIDDAY = datetime(2026, 10, 29, 16, 0, tzinfo=UTC)
THU_EVENING = datetime(2026, 10, 29, 22, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("day", "timing", "flag"),
    [
        (date(2026, 10, 29), "after", True),  # tonight, before Friday's open
        (date(2026, 10, 29), "before", False),  # already out this morning
        (date(2026, 10, 29), "during", True),  # later in today's session
        (date(2026, 10, 30), "before", True),  # Friday before the open
        (date(2026, 10, 30), "after", False),  # after Friday's open
        (date(2026, 10, 30), None, True),  # unknown time on the next session day
        (date(2026, 10, 29), None, True),  # unknown time today, may be after the close
        (date(2026, 11, 2), "before", False),  # next week
    ],
)
def test_during_the_session(day, timing, flag):
    assert _flagged([_event("A.US", day, timing)], THU_MIDDAY) == (["A.US"] if flag else [])


def test_after_the_close_tonight_already_passed():
    # 22:00 UTC is after a 20:00 close + report: tonight's report is out
    assert _flagged([_event("A.US", date(2026, 10, 29), "after")], THU_EVENING) == []
    assert _flagged([_event("A.US", date(2026, 10, 30), "before")], THU_EVENING) == ["A.US"]


def test_weekend_report_is_before_monday_open():
    friday_evening = datetime(2026, 10, 30, 21, 0, tzinfo=UTC)
    saturday = date(2026, 10, 31)
    monday_before = date(2026, 11, 2)
    events = [_event("SAT.US", saturday), _event("MON.US", monday_before, "before")]
    assert _flagged(events, friday_evening) == ["SAT.US", "MON.US"]


def test_warning_carries_the_next_open():
    [w] = earnings_before_next_open(
        [_event("A.US", date(2026, 10, 29), "after")],
        now=THU_MIDDAY,
        calendar_for=lambda t: NYSE,
    )
    assert w.next_open == datetime(2026, 10, 30, 13, 30, tzinfo=UTC)
    assert w.report_date == date(2026, 10, 29)
    assert w.before_after_market == "after"


def test_unknown_calendar_falls_back_to_nyse():
    def unknown(_ticker):
        from stonks.scheduling.calendar import UnknownCalendarError

        raise UnknownCalendarError("nope")

    out = earnings_before_next_open(
        [_event("X", date(2026, 10, 29), "after")], now=THU_MIDDAY, calendar_for=unknown
    )
    assert [w.ticker for w in out] == ["X"]


def test_naive_now_is_refused():
    with pytest.raises(ValueError):
        earnings_before_next_open(
            [], now=datetime(2026, 10, 29, 16, 0), calendar_for=lambda t: NYSE
        )
