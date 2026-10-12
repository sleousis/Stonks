"""The daily call budget: keeps a backfill inside the vendor's allowance.

:class:`CallBudget` is the source's request meter. Before each request it

1. paces requests to ``requests_per_minute``;
2. starts a new day at midnight UTC (when the vendor resets its counter);
3. reads the vendor's own count of today's calls at the start and every
   ``usage_check_seconds`` (reading it costs nothing), so calls made by
   other processes on the same key count too;
4. raises :class:`BudgetExhausted` when the request would take today's
   calls past the budget (or past the plan's limit, or past the run's own
   cap), so the request never leaves.

Fetch threads call it, so the counters sit behind a lock and are kept in
memory; the runner's thread stores them with :meth:`flush`.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from stonks.ingest.ensure import RateLimiter
from stonks.ingest.sources.base import ApiUsage
from stonks.logging import get_logger

_log = get_logger("stonks.ingest.backfill.budget")


class BudgetExhausted(Exception):
    """No room for the next request. ``resets_at`` is when the daily
    budget refills (``None``: the run's own cap, which never refills)."""

    def __init__(self, reason: str, resets_at: datetime | None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.resets_at = resets_at


@dataclass
class _Tracker:
    calls: int = 0


@dataclass(frozen=True)
class Flushed:
    """Calls of one day not stored yet, and the vendor's readings that day
    as ``(used, limit, read_at)``."""

    day: date
    calls: int
    readings: list[tuple[int, int | None, datetime]]


@dataclass
class _Day:
    day: date
    #: The vendor's count at the last read (``None``: never read today).
    vendor_used: int | None = None
    vendor_limit: int | None = None
    read_at: datetime | None = None
    #: Calls this process made since that read (or all day without one).
    since_read: int = 0
    #: Calls this process made today, all told.
    spent: int = 0
    #: Spent calls not stored yet.
    unflushed: int = 0
    stopped: bool = False
    readings: list[tuple[int, int | None, datetime]] = field(default_factory=list)


def next_reset(moment: datetime, margin: timedelta = timedelta()) -> datetime:
    """Midnight UTC after ``moment``, plus ``margin``."""
    day = moment.astimezone(UTC).date() + timedelta(days=1)
    return datetime(day.year, day.month, day.day, tzinfo=UTC) + margin


class CallBudget:
    def __init__(
        self,
        *,
        daily_budget: int,
        usage: Callable[[], ApiUsage | None] | None = None,
        spent_today: Callable[[date], int] | None = None,
        run_cap: int | None = None,
        requests_per_minute: int = 900,
        usage_check_seconds: float = 600.0,
        reset_margin: timedelta = timedelta(minutes=10),
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """``usage`` reads the vendor's count (``None`` or a failing read:
        count locally). ``spent_today`` is what earlier runs stored for a
        day, used when the vendor cannot be read."""
        self._budget = daily_budget
        self._usage = usage
        self._spent_today = spent_today
        self._run_cap = run_cap
        self._check_every = timedelta(seconds=usage_check_seconds)
        self._margin = reset_margin
        self._clock = clock or (lambda: datetime.now(UTC))
        self._limiter = RateLimiter(requests_per_minute / 60.0, clock=monotonic, sleep=sleep)
        self._lock = threading.RLock()
        self._local = threading.local()
        self._run_spent = 0
        self._carried: list[Flushed] = []
        self._day = self._new_day(self._clock().date())
        self._reading = False

    # ---- the meter ----------------------------------------------------------------

    def before_request(self, endpoint: str, cost: int) -> None:
        if cost > 0:
            with self._lock:
                self._charge(endpoint, cost)
            tracker: _Tracker | None = getattr(self._local, "tracker", None)
            if tracker is not None:
                tracker.calls += cost
        self._limiter.acquire()

    def _charge(self, endpoint: str, cost: int) -> None:
        now = self._clock()
        self._roll(now)
        day = self._day
        if day.read_at is None or now - day.read_at >= self._check_every:
            self._read_vendor(now)
        if self._run_cap is not None and self._run_spent + cost > self._run_cap:
            raise BudgetExhausted(f"the run's cap of {self._run_cap} calls is reached", None)
        if day.stopped:
            raise BudgetExhausted("the vendor says today's allowance is used", self.resets_at)
        used = self.used_today
        ceiling = self.ceiling
        if used + cost > ceiling:
            raise BudgetExhausted(
                f"{used} of {ceiling} calls used today, {endpoint} costs {cost}", self.resets_at
            )
        day.since_read += cost
        day.spent += cost
        day.unflushed += cost
        self._run_spent += cost

    # ---- state ---------------------------------------------------------------------

    @property
    def used_today(self) -> int:
        """Today's calls: the vendor's last count plus ours since, or our
        own count when the vendor could not be read."""
        day = self._day
        if day.vendor_used is not None:
            return day.vendor_used + day.since_read
        return day.since_read

    @property
    def ceiling(self) -> int:
        limit = self._day.vendor_limit
        return min(self._budget, limit) if limit else self._budget

    @property
    def remaining(self) -> int:
        with self._lock:
            now = self._clock()
            self._roll(now)
            if self._day.read_at is None:
                self._read_vendor(now)
            room = max(0, self.ceiling - self.used_today)
            if self._run_cap is not None:
                room = min(room, max(0, self._run_cap - self._run_spent))
            return room

    @property
    def run_spent(self) -> int:
        return self._run_spent

    @property
    def resets_at(self) -> datetime:
        return next_reset(self._clock(), self._margin)

    def ensure_room(self, calls: int) -> None:
        """Raise :class:`BudgetExhausted` unless ``calls`` more fit today."""
        if calls <= 0:
            return
        if self.remaining < calls:
            if self._run_cap is not None and self._run_spent + calls > self._run_cap:
                raise BudgetExhausted(f"the run's cap of {self._run_cap} calls is reached", None)
            raise BudgetExhausted(
                f"{self.used_today} of {self.ceiling} calls used today, the next work needs "
                f"about {calls}",
                self.resets_at,
            )

    def exhaust(self) -> None:
        """The vendor refused a request for the day (HTTP 402)."""
        with self._lock:
            self._day.stopped = True

    @contextmanager
    def track(self) -> Iterator[_Tracker]:
        """Count the calls this thread makes inside the block."""
        tracker = _Tracker()
        self._local.tracker = tracker
        try:
            yield tracker
        finally:
            self._local.tracker = None

    def flush(self) -> list[Flushed]:
        """Calls and vendor readings not stored yet, per day (a day that
        ended since the last flush comes first); the caller stores them."""
        with self._lock:
            out = list(self._carried)
            self._carried.clear()
            day = self._day
            if day.unflushed or day.readings:
                out.append(Flushed(day.day, day.unflushed, list(day.readings)))
            day.unflushed = 0
            day.readings.clear()
            return out

    def refresh(self) -> None:
        """Read the vendor's count now (at the start of a new day)."""
        with self._lock:
            now = self._clock()
            self._roll(now)
            self._read_vendor(now)

    # ---- internals -----------------------------------------------------------------

    def _new_day(self, day: date) -> _Day:
        stored = self._spent_today(day) if self._spent_today is not None else 0
        return _Day(day=day, since_read=stored)

    def _roll(self, now: datetime) -> None:
        today = now.astimezone(UTC).date()
        if today != self._day.day:
            # the vendor's counter reset at midnight UTC; keep what is not stored yet
            left = self._day
            if left.unflushed or left.readings:
                self._carried.append(Flushed(left.day, left.unflushed, list(left.readings)))
            self._day = _Day(day=today)

    def _read_vendor(self, now: datetime) -> None:
        day = self._day
        day.read_at = now
        if self._usage is None or self._reading:
            return
        self._reading = True
        try:
            usage = self._usage()
        except Exception as exc:  # a failed read must not stop the run
            _log.warning("backfill.usage_unavailable", error=type(exc).__name__)
            return
        finally:
            self._reading = False
        if usage is None:
            return
        if usage.day is not None and usage.day != day.day:
            # the vendor still counts yesterday: its counter has not reset yet
            used = usage.used_today if usage.day > day.day else 0
        else:
            used = usage.used_today
        day.vendor_used = used
        day.vendor_limit = usage.daily_limit
        day.since_read = 0
        day.readings.append((used, usage.daily_limit, now))
        _log.info(
            "backfill.usage",
            used_today=used,
            daily_limit=usage.daily_limit,
            ceiling=self.ceiling,
        )
