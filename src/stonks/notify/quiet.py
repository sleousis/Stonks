"""Quiet hours: a daily wall-clock window in the user's own time zone.

During quiet hours ``low`` and ``normal`` deliveries wait until the window
ends (then go out as a digest); ``high`` always goes. Times are compared in
the user's zone (``users.timezone``), never in UTC, and the window may cross
midnight (``22:00``-``07:00``). ``start == end`` means no quiet hours.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from stonks.logging import get_logger

_log = get_logger("stonks.notify.quiet")
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def parse_hhmm(value: str) -> time:
    """``"HH:MM"`` (24 h) to a :class:`time`; anything else raises ValueError."""
    m = _HHMM.match(value or "")
    if not m:
        raise ValueError(f"expected HH:MM (00:00-23:59), got {value!r}")
    return time(int(m.group(1)), int(m.group(2)))


def user_zone(name: str | None) -> ZoneInfo:
    """The user's zone, or UTC (logged) when the stored name is unknown."""
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            _log.warning("notify.quiet.unknown_timezone", timezone=name[:64])
    return ZoneInfo("UTC")


@dataclass(frozen=True)
class QuietHours:
    start: time
    end: time
    timezone: str = "UTC"

    @property
    def zone(self) -> ZoneInfo:
        return user_zone(self.timezone)

    def is_quiet(self, now: datetime) -> bool:
        if self.start == self.end:
            return False
        t = now.astimezone(self.zone).time().replace(tzinfo=None)
        if self.start < self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end

    def ends_after(self, now: datetime) -> datetime:
        """When the window containing ``now`` ends (UTC); ``now`` itself when
        ``now`` isn't in quiet hours."""
        if not self.is_quiet(now):
            return now
        zone = self.zone
        local = now.astimezone(zone)
        day = local.date()
        if self.start > self.end and local.time().replace(tzinfo=None) >= self.start:
            day += timedelta(days=1)  # evening part: ends tomorrow morning
        end = datetime.combine(day, self.end, tzinfo=zone).astimezone(UTC)
        # A wall time in a DST gap or fold can map to an instant at or before
        # now; never hand back a time in the past.
        return end if end > now else now + timedelta(minutes=1)
