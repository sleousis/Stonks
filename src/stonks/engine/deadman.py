"""The engine dead-man (roadmap 21.3.4).

A live engine dispatches a bar close every minute while its market is open.
When none was dispatched for ``deadman_minutes`` in market hours, the
engine is stuck, its stream is dead, or its process is gone. The scheduler's
watchdog runs :class:`EngineDeadman` next to its
:class:`~stonks.scheduling.deadman.DeadlineWatchdog` and alerts the
operator through the existing ``Notifier``.

- Silence counts from the latest of the last dispatch, the engine's start
  and today's open, so yesterday's last bar never alerts at the open.
- A stopped engine never alerts. A closed market never alerts.
- One alert per silent stretch, deduplicated in the scheduler's
  ``scheduler_deadline_alerts`` table (job ``engine:<id>``), so a restart
  of the scheduler does not repeat it. The next stretch alerts again.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from stonks.engine.status import EngineStatus, EngineStatusStore
from stonks.logging import get_logger
from stonks.notify import Notification, Notifier
from stonks.scheduling.calendar import MarketCalendar, Session, get_calendar
from stonks.scheduling.runs import RunStore

if TYPE_CHECKING:
    from stonks.config import Settings

_log = get_logger("stonks.engine.deadman")

CalendarFor = Callable[[str], MarketCalendar]

#: Prefix of the dead-man's rows in ``scheduler_deadline_alerts``.
ALERT_JOB_PREFIX = "engine:"


@dataclass(frozen=True)
class EngineSilence:
    engine_id: str
    #: Silence counts from here.
    since: datetime
    silent_seconds: float
    last_dispatch_at: datetime | None

    @property
    def key(self) -> str:
        """Stable for one silent stretch, new for the next."""
        return self.since.isoformat()


def session_at(calendar: MarketCalendar, when: datetime) -> Session | None:
    """The session open at ``when``, if any."""
    day = when.date()
    for d in (day - timedelta(days=1), day, day + timedelta(days=1)):
        s = calendar.session(d)
        if s is not None and s.open <= when < s.close:
            return s
    return None


def _calendar(name: str, calendar_for: CalendarFor, fallback: str) -> MarketCalendar | None:
    for candidate in (name, fallback):
        try:
            return calendar_for(candidate)
        except Exception as exc:  # an unknown calendar name: try the default
            _log.warning("engine.deadman_calendar_failed", calendar=candidate, error=str(exc))
    return None


def silence_of(
    status: EngineStatus,
    now: datetime,
    *,
    minutes: int,
    calendar_for: CalendarFor = get_calendar,
    fallback_calendar: str = "XNYS",
) -> EngineSilence | None:
    """The engine's silence when it is past the limit, else ``None``."""
    if status.stopped_at is not None:
        return None
    calendar = _calendar(status.calendar, calendar_for, fallback_calendar)
    if calendar is None:
        return None
    session = session_at(calendar, now)
    if session is None:
        return None
    marks = [status.started_at, session.open]
    if status.last_dispatch_at is not None:
        marks.append(status.last_dispatch_at)
    since = max(marks)
    silent = (now - since).total_seconds()
    if silent < minutes * 60:
        return None
    return EngineSilence(status.engine_id, since, silent, status.last_dispatch_at)


def silent_engines(
    statuses: Sequence[EngineStatus],
    now: datetime,
    *,
    minutes: int,
    calendar_for: CalendarFor = get_calendar,
    fallback_calendar: str = "XNYS",
) -> list[EngineSilence]:
    out: list[EngineSilence] = []
    for status in statuses:
        silence = silence_of(
            status,
            now,
            minutes=minutes,
            calendar_for=calendar_for,
            fallback_calendar=fallback_calendar,
        )
        if silence is not None:
            out.append(silence)
    return out


class EngineDeadman:
    """Alerts once per silent stretch of each engine."""

    def __init__(
        self,
        state_path: str | Path,
        store: RunStore,
        notifier: Notifier,
        *,
        minutes: int = 5,
        calendar_for: CalendarFor = get_calendar,
        fallback_calendar: str = "XNYS",
    ) -> None:
        self._engines = EngineStatusStore(state_path)
        self._store = store
        self._notifier = notifier
        self.minutes = minutes
        self._calendar_for = calendar_for
        self._fallback = fallback_calendar

    def check(self, now: datetime) -> list[EngineSilence]:
        """The silences alerted on by this call."""
        alerted: list[EngineSilence] = []
        silences = silent_engines(
            self._engines.read_all(),
            now,
            minutes=self.minutes,
            calendar_for=self._calendar_for,
            fallback_calendar=self._fallback,
        )
        for s in silences:
            if not self._store.mark_deadline_alerted(
                ALERT_JOB_PREFIX + s.engine_id, s.key, now=now
            ):
                continue
            minutes = int(s.silent_seconds // 60)
            last = s.last_dispatch_at.isoformat() if s.last_dispatch_at else "never"
            _log.error(
                "engine.deadman_silent",
                engine=s.engine_id,
                since=s.since.isoformat(),
                minutes=minutes,
            )
            self._notifier.notify(
                Notification(
                    level="error",
                    title=f"live engine {s.engine_id} is silent",
                    message=(
                        f"No bar close for {minutes} minutes while the market is open "
                        f"(last dispatch: {last}). Check the stream and the engine process. "
                        "New entries wait until prices flow again."
                    ),
                    fields={
                        "engine": s.engine_id,
                        "since": s.since.isoformat(),
                        "last_dispatch_at": last,
                        "silent_minutes": minutes,
                    },
                )
            )
            alerted.append(s)
        return alerted


def engine_deadman_from_settings(
    settings: Settings, store: RunStore, notifier: Notifier
) -> EngineDeadman:
    """The dead-man the scheduler's watchdog runs (``[streaming.monitor]``).
    With no engine row it never alerts, so it is always on."""
    return EngineDeadman(
        settings.state.path,
        store,
        notifier,
        minutes=settings.streaming.monitor.deadman_minutes,
        fallback_calendar=settings.streaming.calendar,
    )
