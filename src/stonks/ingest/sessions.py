"""When a daily bar is final: session closes for the ingest path.

A daily bar is only final once its session has closed. Before that the
vendor's "today" bar holds the latest trade as ``close`` and part of the
day's volume. The ingest path uses :class:`SessionCloses` to

- drop daily bars whose session has not closed yet (they are fetched
  again once it has), and
- skip fetching date ranges that hold no closed session (weekends,
  holidays, today before the close).

:class:`MarketSessionCloses` reads the market calendars (holidays, early
closes, crypto 24/7). When a ticker has no known calendar, or the dates
are outside it, :func:`fallback_closes` counts every weekday (every day
for crypto) as a session that closes at the next UTC midnight.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol

import pandas as pd

__all__ = [
    "MarketSessionCloses",
    "SessionCloses",
    "closed_sessions",
    "default_sessions",
    "drop_open_sessions",
    "fallback_closes",
    "is_final",
    "open_sessions",
    "session_close",
]


class SessionCloses(Protocol):
    """Session dates of ``ticker``'s market in ``[start, end]`` mapped to
    their UTC close, or ``None`` when the market is not known."""

    def closes(
        self, ticker: str, start: date, end: date, asset_class: str | None = None
    ) -> dict[date, datetime] | None: ...


class MarketSessionCloses:
    """:class:`SessionCloses` over the scheduler's market calendars. The
    import is lazy so the ingest block does not load the scheduler at
    import time. Answers are cached per calendar and range, keeping the
    ``max_entries`` most recently used (BE-62)."""

    def __init__(self, max_entries: int = 1024) -> None:
        self._cache: OrderedDict[tuple[str, date, date], dict[date, datetime] | None] = (
            OrderedDict()
        )
        self._max_entries = max(1, int(max_entries))
        self._lock = threading.Lock()

    def closes(
        self, ticker: str, start: date, end: date, asset_class: str | None = None
    ) -> dict[date, datetime] | None:
        from stonks.scheduling.calendar import (
            CalendarRangeError,
            UnknownCalendarError,
            calendar_for_ticker,
        )

        try:
            cal = calendar_for_ticker(ticker, asset_class)
        except UnknownCalendarError:
            return None
        key = (cal.name, start, end)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        first = getattr(cal, "first_session", None)
        last = getattr(cal, "last_session", None)
        out: dict[date, datetime] | None
        if (first is not None and start < first) or (last is not None and end > last):
            out = None
        else:
            try:
                out = {s.date: s.close for s in cal.sessions(start, end)}
            except CalendarRangeError:
                out = None
        with self._lock:
            self._cache[key] = out
            self._cache.move_to_end(key)
            while len(self._cache) > self._max_entries:
                self._cache.popitem(last=False)
        return out


_DEFAULT: MarketSessionCloses | None = None
_DEFAULT_LOCK = threading.Lock()


def default_sessions() -> MarketSessionCloses:
    """The process-wide :class:`MarketSessionCloses` (one shared cache)."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = MarketSessionCloses()
        return _DEFAULT


def fallback_closes(start: date, end: date, *, crypto: bool = False) -> dict[date, datetime]:
    """Every weekday (every day for crypto) in ``[start, end]``, closing at
    the following UTC midnight."""
    out: dict[date, datetime] = {}
    day = start
    while day <= end:
        if crypto or day.weekday() < 5:
            out[day] = datetime.combine(day + timedelta(days=1), time(), UTC)
        day += timedelta(days=1)
    return out


def _closes(
    sessions: SessionCloses | None,
    ticker: str,
    start: date,
    end: date,
    asset_class: str | None,
) -> dict[date, datetime]:
    known = sessions.closes(ticker, start, end, asset_class) if sessions is not None else None
    if known is None:
        return fallback_closes(start, end, crypto=asset_class == "crypto")
    return known


def closed_sessions(
    sessions: SessionCloses | None,
    ticker: str,
    start: date,
    end: date,
    now: datetime,
    asset_class: str | None = None,
) -> list[date]:
    """Session dates in ``[start, end]`` whose close is at or before ``now``."""
    closes = _closes(sessions, ticker, start, end, asset_class)
    return sorted(d for d, close in closes.items() if close <= now)


def open_sessions(
    sessions: SessionCloses | None,
    ticker: str,
    start: date,
    end: date,
    now: datetime,
    asset_class: str | None = None,
) -> list[date]:
    """Session dates in ``[start, end]`` whose close is after ``now``."""
    closes = _closes(sessions, ticker, start, end, asset_class)
    return sorted(d for d, close in closes.items() if close > now)


def is_final(
    sessions: SessionCloses | None,
    ticker: str,
    days: list[date],
    now: datetime,
    asset_class: str | None = None,
) -> list[bool]:
    """For each daily bar date: whether its session has closed by ``now``.
    A date with no session on the calendar (a vendor bar on a holiday)
    counts as closed at the next UTC midnight."""
    if not days:
        return []
    closes = _closes(sessions, ticker, min(days), max(days), asset_class)
    out = []
    for d in days:
        close = closes.get(d) or datetime.combine(d + timedelta(days=1), time(), UTC)
        out.append(close <= now)
    return out


def session_close(
    sessions: SessionCloses | None, ticker: str, day: date, asset_class: str | None = None
) -> datetime:
    """The UTC close of ``day``'s session (the next UTC midnight when the
    calendar has no session that day)."""
    close = _closes(sessions, ticker, day, day, asset_class).get(day)
    return close or datetime.combine(day + timedelta(days=1), time(), UTC)


def drop_open_sessions(
    frame: pd.DataFrame,
    sessions: SessionCloses | None,
    ticker: str,
    now: datetime,
    asset_class: str | None = None,
) -> pd.DataFrame:
    """``frame`` (daily bars with a ``date`` or ``timestamp`` column)
    without the rows whose session has not closed by ``now``."""
    if frame.empty:
        return frame
    col = frame["date"] if "date" in frame.columns else frame["timestamp"]
    days = [pd.Timestamp(v).date() for v in col]
    keep = is_final(sessions, ticker, days, now, asset_class)
    return frame[keep] if not all(keep) else frame
