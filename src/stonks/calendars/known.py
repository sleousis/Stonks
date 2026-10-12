"""When an earnings report is known, for point-in-time reads (P12).

The earnings calendar gives a report day and a timing (``before`` the
open, ``during`` the session, ``after`` the close, or none), not a time.
A report counts from the latest moment its timing allows on any market
from Asia to the Americas, in naive UTC like every lake timestamp:

- ``before``: 14:30, the latest regular open (New York in winter);
- ``during``: 21:00, the latest regular close (New York in winter);
- ``after`` or no timing: the end of the report day.

So a daily decision on the report day sees every report of that day (it
is made at the close and fills at the next open), while an intraday
decision sees a report only once that moment has passed. Unlike
:mod:`stonks.calendars.timing` this needs no market calendar, so the
point-in-time lake and the strategies can use it on any row.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

import pandas as pd

__all__ = ["known_moments", "report_known_at"]

_OFFSETS: dict[str, timedelta] = {
    "before": timedelta(hours=14, minutes=30),
    "during": timedelta(hours=21),
}
_END_OF_DAY = timedelta(days=1)


def _day(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    stamp = pd.Timestamp(value)
    if not isinstance(stamp, pd.Timestamp) or pd.isna(stamp):
        raise ValueError(f"no report date in {value!r}")
    return date.fromordinal(stamp.toordinal())


def report_known_at(report_date: Any, timing: str | None) -> datetime:
    """The naive UTC moment from which a report on ``report_date`` with
    ``timing`` counts as known (module doc)."""
    start = datetime.combine(_day(report_date), time.min)
    return start + _OFFSETS.get(str(timing), _END_OF_DAY)


def known_moments(frame: pd.DataFrame) -> pd.Series:
    """:func:`report_known_at` for every row of an earnings calendar frame
    (``report_date`` and ``before_after_market``), NaT where the day is
    missing."""
    days = pd.to_datetime(frame["report_date"], errors="coerce")
    timing = frame["before_after_market"].astype(object)
    offsets = pd.to_timedelta([_OFFSETS.get(str(t), _END_OF_DAY) for t in timing])
    return pd.Series(days.to_numpy() + offsets.to_numpy(), index=frame.index)
