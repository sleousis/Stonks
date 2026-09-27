"""The clock seam (roadmap 19.1).

Phase 19 code asks a :class:`Clock` for the time instead of calling
``datetime.now()`` itself, so tests can pin or advance time without
patching. :data:`SYSTEM_CLOCK` is the real one. :class:`FakeClock` starts at
a fixed moment and moves only when told to.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """The current time, timezone-aware UTC."""
        ...


def _utc(when: datetime) -> datetime:
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    """A clock for tests: fixed until :meth:`advance` or :meth:`set`."""

    def __init__(self, start: datetime) -> None:
        self._now = _utc(start)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> datetime:
        self._now = self._now + delta
        return self._now

    def set(self, when: datetime) -> None:
        self._now = _utc(when)


class FixedClock:
    """Always the same moment: a scheduled run's fire time, say."""

    def __init__(self, when: datetime) -> None:
        self._when = _utc(when)

    def now(self) -> datetime:
        return self._when


SYSTEM_CLOCK: Clock = SystemClock()


def today(clock: Clock) -> date:
    """The clock's current UTC date."""
    return clock.now().date()


def iso_now(clock: Clock) -> str:
    """The clock's current time as an ISO string to the second."""
    return clock.now().isoformat(timespec="seconds")
