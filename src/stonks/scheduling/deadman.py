"""Dead-man's switch (roadmap 12.3): outside and inside checks that the
scheduled jobs actually ran.

- **Outside**: a :class:`Pinger` tells an external monitor (healthchecks.io,
  Uptime Kuma, ...) when a job starts, succeeds or fails. The monitor
  alerts when pings stop arriving, which also covers the scheduler process
  itself dying, the one failure nothing inside it can report.
- **Inside**: :class:`DeadlineWatchdog` checks, per job with a deadline,
  that the latest fire whose deadline has passed has a ``succeeded`` or
  ``skipped`` run, and alerts through the existing ``Notifier`` once per
  missed run otherwise (never started, still running, or failed).

Ping URLs embed a check token, so they are only ever logged redacted.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

from stonks.logging import get_logger
from stonks.notify import Notification, Notifier, redact_url
from stonks.scheduling.jobs import JobSpec
from stonks.scheduling.runs import DONE_STATUSES, RunStore
from stonks.scheduling.triggers import Fire

PingEvent = Literal["start", "success", "fail"]

#: How far back the watchdog looks for a job's latest due fire. Covers the
#: longest gap between sessions (a long holiday) plus a weekend.
WATCHDOG_LOOKBACK = timedelta(days=10)

#: Body bytes sent with a ping (healthchecks.io stores up to 100 kB).
_MAX_BODY = 10_000

_log = get_logger("stonks.scheduling.deadman")


class Pinger(ABC):
    """Sends dead-man pings. ``ping`` never raises: a monitor outage must
    not fail the job it reports on."""

    def ping(self, url: str, event: PingEvent, *, run_id: str, body: str = "") -> None:
        try:
            self._send(url, event, run_id=run_id, body=body[:_MAX_BODY])
        except Exception as exc:
            _log.warning(
                "deadman.ping_failed",
                url=redact_url(url),
                ping_event=event,
                error_type=type(exc).__name__,
            )

    @abstractmethod
    def _send(self, url: str, event: PingEvent, *, run_id: str, body: str) -> None: ...


class NullPinger(Pinger):
    def _send(self, url: str, event: PingEvent, *, run_id: str, body: str) -> None:
        return None


def ping_target(url: str, event: PingEvent) -> str:
    """healthchecks.io convention: ``<url>/start``, ``<url>``, ``<url>/fail``."""
    base = url.rstrip("/")
    return {"start": f"{base}/start", "success": base, "fail": f"{base}/fail"}[event]


class HttpPinger(Pinger):
    """POSTs to the healthchecks.io-style endpoint for the event, with the
    run id as ``rid`` so the monitor can pair start and finish."""

    def __init__(self, timeout_seconds: float = 5.0, session: Any = None) -> None:
        import requests

        self._timeout = timeout_seconds
        self._session = session if session is not None else requests.Session()

    def _send(self, url: str, event: PingEvent, *, run_id: str, body: str) -> None:
        resp = self._session.post(
            ping_target(url, event),
            params={"rid": run_id},
            data=body.encode("utf-8"),
            timeout=self._timeout,
        )
        resp.raise_for_status()


# ---- deadline watchdog ------------------------------------------------------------


@dataclass(frozen=True)
class MissedRun:
    job_name: str
    fire: Fire
    deadline_at: datetime
    #: The run's status, or None when it never started.
    status: str | None

    @property
    def reason(self) -> str:
        if self.status is None:
            return "did not run"
        if self.status == "running":
            return "still running"
        return self.status


def missed_deadlines(
    specs: Sequence[JobSpec],
    store: RunStore,
    now: datetime,
    *,
    not_before: datetime | None = None,
) -> list[MissedRun]:
    """Jobs whose latest fire with a passed deadline has no ``succeeded``
    or ``skipped`` run. Fires before ``not_before`` (the scheduler's first
    ever start) are ignored, so a fresh install doesn't alert on history."""
    out = []
    for spec in specs:
        if spec.deadline is None:
            continue
        fire = spec.trigger.last_fire_at_or_before(now - spec.deadline, WATCHDOG_LOOKBACK)
        if fire is None or (not_before is not None and fire.scheduled_for < not_before):
            continue
        run = store.get(spec.name, fire.key)
        if run is not None and run.status in DONE_STATUSES:
            continue
        out.append(
            MissedRun(
                job_name=spec.name,
                fire=fire,
                deadline_at=fire.scheduled_for + spec.deadline,
                status=run.status if run is not None else None,
            )
        )
    return out


class WatchdogCheck(Protocol):
    """Another check the watchdog runs on each wake-up (the engine
    dead-man, roadmap 21.3.4). It alerts on its own."""

    def check(self, now: datetime) -> object: ...


class DeadlineWatchdog:
    """Alerts once per missed run (deduplicated in the state DB), then runs
    each of ``extra_checks``. A check that raises is logged, and the
    others still run."""

    def __init__(
        self,
        specs: Sequence[JobSpec],
        store: RunStore,
        notifier: Notifier,
        *,
        extra_checks: Sequence[WatchdogCheck] = (),
    ) -> None:
        self._specs = list(specs)
        self._store = store
        self._notifier = notifier
        self._extra = list(extra_checks)

    def check(self, now: datetime) -> list[MissedRun]:
        """The missed runs alerted on by this call."""
        alerted = self._check_deadlines(now)
        for extra in self._extra:
            try:
                extra.check(now)
            except Exception as exc:  # one broken check must not hide the others
                _log.error(
                    "deadman.extra_check_failed",
                    check=type(extra).__name__,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
        return alerted

    def _check_deadlines(self, now: datetime) -> list[MissedRun]:
        alerted: list[MissedRun] = []
        not_before = self._store.first_started_at()
        for miss in missed_deadlines(self._specs, self._store, now, not_before=not_before):
            if not self._store.mark_deadline_alerted(miss.job_name, miss.fire.key, now=now):
                continue
            _log.error(
                "deadman.deadline_missed",
                job=miss.job_name,
                run_key=miss.fire.key,
                reason=miss.reason,
            )
            self._notifier.notify(
                Notification(
                    level="error",
                    title=f"scheduled job {miss.job_name} missed its deadline",
                    message=(
                        f"{miss.job_name} for {miss.fire.as_of.isoformat()} "
                        f"(scheduled {miss.fire.scheduled_for.isoformat()}): {miss.reason} "
                        f"by {miss.deadline_at.isoformat()}"
                    ),
                    fields={
                        "job": miss.job_name,
                        "run_key": miss.fire.key,
                        "scheduled_for": miss.fire.scheduled_for.isoformat(),
                        "deadline_at": miss.deadline_at.isoformat(),
                        "status": miss.status,
                    },
                )
            )
            alerted.append(miss)
        return alerted
