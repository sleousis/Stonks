"""Triggers: when a scheduled job fires (roadmap 12.2).

A ``Trigger`` turns a time window into the ``Fire`` instants inside it.
Three kinds cover the daily loop:

- :class:`SessionTrigger`: relative to a market session's open or close,
  on that calendar's trading days only ("NYSE close + 30 min"). DST and
  early closes come from the calendar, so the UTC fire time moves with
  them.
- :class:`DailyTrigger`: a wall-clock time in a named timezone, optionally
  limited to weekdays or to a calendar's trading days.
- :class:`IntervalTrigger`: every N minutes, anchored at the Unix epoch so
  restarts never shift the grid.

Each ``Fire`` carries a ``key`` that identifies the run: the session or
local date for the date-based triggers (so changing an offset in the
config can never run the same day twice) and the UTC instant for interval
triggers. The scheduler stores one run per ``(job, key)``.

Windows are start-exclusive and end-inclusive: ``(start, end]``. A caller
that advances ``start`` to the previous ``end`` therefore sees every fire
exactly once.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from stonks.scheduling.calendar import CalendarRangeError, ensure_utc, get_calendar

#: How far ``next_fire`` looks ahead before concluding there is none.
_LOOKAHEAD = timedelta(days=60)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

SessionAnchor = Literal["open", "close"]


@dataclass(frozen=True)
class Fire:
    scheduled_for: datetime  # UTC
    as_of: date  # the trading / local date the run is for
    key: str  # unique per trigger; the run's idempotency key


class Trigger(ABC):
    @abstractmethod
    def fires_between(self, start: datetime, end: datetime) -> list[Fire]:
        """Fires with ``start < scheduled_for <= end``, in order."""

    def next_fire(self, after: datetime) -> Fire | None:
        """The first fire strictly after ``after`` (None when the trigger
        has nothing in the next 60 days, e.g. past a calendar's range)."""
        after = ensure_utc(after)
        fires = self.fires_between(after, after + _LOOKAHEAD)
        return fires[0] if fires else None

    def last_fire_at_or_before(self, when: datetime, lookback: timedelta) -> Fire | None:
        """The latest fire with ``when - lookback < scheduled_for <= when``."""
        when = ensure_utc(when)
        fires = self.fires_between(when - lookback, when)
        return fires[-1] if fires else None

    @abstractmethod
    def describe(self) -> str: ...


@dataclass(frozen=True)
class SessionTrigger(Trigger):
    calendar: str
    anchor: SessionAnchor = "close"
    offset: timedelta = timedelta(0)

    def fires_between(self, start: datetime, end: datetime) -> list[Fire]:
        start, end = ensure_utc(start), ensure_utc(end)
        if end <= start:
            return []
        cal = get_calendar(self.calendar)
        # Sessions whose (edge + offset) can fall in the window: widen by
        # the offset and one day each side for local-vs-UTC date shifts.
        slack = abs(self.offset) + timedelta(days=1)
        try:
            sessions = cal.sessions((start - slack).date(), (end + slack).date())
        except CalendarRangeError:
            return []
        out = []
        for s in sessions:
            at = (s.open if self.anchor == "open" else s.close) + self.offset
            if start < at <= end:
                out.append(Fire(at, s.date, s.date.isoformat()))
        return out

    def describe(self) -> str:
        minutes = int(self.offset.total_seconds() // 60)
        sign = "+" if minutes >= 0 else "-"
        return f"{self.calendar} {self.anchor} {sign} {abs(minutes)} min on trading days"


@dataclass(frozen=True)
class DailyTrigger(Trigger):
    at: time
    tz: str = "UTC"
    #: 0 = Monday ... 6 = Sunday; None = every day.
    weekdays: frozenset[int] | None = None
    #: Only on this calendar's trading days.
    calendar: str | None = None

    def __post_init__(self) -> None:
        try:
            ZoneInfo(self.tz)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {self.tz!r}") from exc

    def _instant(self, day: date) -> datetime:
        # fold=0: a local time skipped by spring-forward maps through the
        # pre-transition offset (fires once, an hour "late" in local
        # terms); an ambiguous autumn time resolves to its first occurrence.
        local = datetime.combine(day, self.at.replace(tzinfo=None), tzinfo=ZoneInfo(self.tz))
        return local.astimezone(UTC)

    def fires_between(self, start: datetime, end: datetime) -> list[Fire]:
        start, end = ensure_utc(start), ensure_utc(end)
        if end <= start:
            return []
        zone = ZoneInfo(self.tz)
        day = start.astimezone(zone).date() - timedelta(days=1)
        last = end.astimezone(zone).date() + timedelta(days=1)
        cal = get_calendar(self.calendar) if self.calendar else None
        out = []
        while day <= last:
            at = self._instant(day)
            if start < at <= end and self._allowed(day, cal):
                out.append(Fire(at, day, day.isoformat()))
            day += timedelta(days=1)
        return out

    def _allowed(self, day: date, cal: object) -> bool:
        if self.weekdays is not None and day.weekday() not in self.weekdays:
            return False
        if cal is None:
            return True
        try:
            return cal.is_trading_day(day)  # type: ignore[attr-defined]
        except CalendarRangeError:
            return False

    def describe(self) -> str:
        where = f" on {self.calendar} trading days" if self.calendar else ""
        return f"daily at {self.at.strftime('%H:%M')} {self.tz}{where}"


@dataclass(frozen=True)
class IntervalTrigger(Trigger):
    every: timedelta

    def __post_init__(self) -> None:
        if self.every <= timedelta(0):
            raise ValueError(f"interval must be positive, got {self.every}")

    def fires_between(self, start: datetime, end: datetime) -> list[Fire]:
        start, end = ensure_utc(start), ensure_utc(end)
        if end <= start:
            return []
        step = self.every
        k = (start - _EPOCH) // step + 1
        out = []
        at = _EPOCH + k * step
        while at <= end:
            out.append(Fire(at, at.date(), at.isoformat()))
            at += step
        return out

    def last_fire_at_or_before(self, when: datetime, lookback: timedelta) -> Fire | None:
        when = ensure_utc(when)
        at = _EPOCH + ((when - _EPOCH) // self.every) * self.every
        if at <= when - lookback:
            return None
        return Fire(at, at.date(), at.isoformat())

    def describe(self) -> str:
        return f"every {int(self.every.total_seconds() // 60)} min"
