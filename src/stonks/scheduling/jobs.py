"""Scheduled job specs, the executor seam and action registries (roadmap 12.2).

A :class:`JobSpec` is a named action plus its trigger and policies, built
from ``[scheduler.jobs]`` by :func:`build_job_specs`. The scheduler hands
each due run to a :class:`JobExecutor`, which decides *where* the action
runs:

- ``api`` (:mod:`stonks.scheduling.api_backend`): through the running REST
  API's job routes, so the scheduler process never opens the lake;
- ``in_process`` (:mod:`stonks.scheduling.in_process`): on the JobRunner
  inside ``stonks serve``;
- ``local`` (:mod:`stonks.scheduling.local`): in the scheduler's own
  process, opening the stores like the CLI does.

Each backend keeps an :class:`ActionRegistry` of the actions it can run,
keyed by name; adding a job kind is one ``@registry.register``. The
scheduler never knows what an action does, and an action never knows how
it was triggered.

Actions return ``failed`` (rather than raising) when they already sent
their own alert, so the scheduler doesn't alert twice; an exception means
the scheduler alerts.
"""

from __future__ import annotations

import os
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from stonks.scheduling.calendar import universe_trades_on
from stonks.scheduling.config import (
    CatchUpPolicy,
    DailyTriggerConfig,
    IntervalTriggerConfig,
    SchedulerConfig,
    SessionTriggerConfig,
)
from stonks.scheduling.triggers import (
    DailyTrigger,
    Fire,
    IntervalTrigger,
    SessionTrigger,
    Trigger,
)

if TYPE_CHECKING:
    from stonks.config import Settings
    from stonks.notify import Notifier

#: The actor recorded on every scheduled run (see docs/design/accounts-and-modes.md).
SCHEDULER_ACTOR = "service:scheduler"

JobRunStatus = Literal["succeeded", "skipped", "failed"]


@dataclass(frozen=True)
class JobSpec:
    name: str
    action: str
    trigger: Trigger
    params: Mapping[str, Any] = field(default_factory=dict)
    deadline: timedelta | None = None
    catch_up: CatchUpPolicy = "latest"
    ping_url: str | None = None


@dataclass(frozen=True)
class RunContext:
    spec: JobSpec
    fire: Fire
    run_id: str
    now: datetime
    settings: Settings
    notifier: Notifier
    #: The executor running this action (backends reach their client here).
    executor: JobExecutor | None = None

    @property
    def params(self) -> Mapping[str, Any]:
        return self.spec.params


@dataclass(frozen=True)
class JobOutcome:
    status: JobRunStatus
    detail: dict[str, Any] = field(default_factory=dict)
    #: The action already alerted (the tick and health notify on their own).
    alerted: bool = False


JobAction = Callable[[RunContext], JobOutcome]


class UnknownActionError(KeyError):
    pass


class ActionRegistry:
    """Actions one backend can run, by name."""

    def __init__(self, backend: str) -> None:
        self.backend = backend
        self._actions: dict[str, JobAction] = {}

    def register(self, name: str) -> Callable[[JobAction], JobAction]:
        def deco(fn: JobAction) -> JobAction:
            self._actions[name] = fn
            return fn

        return deco

    def get(self, name: str) -> JobAction:
        try:
            return self._actions[name]
        except KeyError:
            raise UnknownActionError(
                f"the {self.backend} backend has no action {name!r}; "
                f"registered: {sorted(self._actions)}"
            ) from None

    def names(self) -> list[str]:
        return sorted(self._actions)


class JobExecutor(ABC):
    """Where a due run's action executes. ``execute`` may raise: the
    scheduler records the run as failed and alerts."""

    backend: str

    @abstractmethod
    def actions(self) -> set[str]: ...

    @abstractmethod
    def execute(self, ctx: RunContext) -> JobOutcome: ...

    def close(self) -> None:  # noqa: B027 - optional hook
        """Release clients or connections (called when the scheduler stops)."""

    def bind_stop(self, stop: threading.Event) -> None:  # noqa: B027 - optional hook
        """The scheduler's stop event, for executors that wait on jobs."""


# ---- helpers shared by the backends ------------------------------------------------


def job_universe(ctx: RunContext) -> list[str]:
    """``params.tickers``, else ``[production].universe``."""
    tickers = ctx.params.get("tickers")
    if tickers:
        return [str(t) for t in tickers]
    return list(ctx.settings.production.universe)


def closed_day_outcome(
    ctx: RunContext, universe: list[str], asset_classes: Mapping[str, str]
) -> JobOutcome | None:
    """A ``skipped`` outcome when no instrument in the universe trades on
    the fire's date (``skip_closed_days = false`` in params disables it).
    With ``asset_classes`` empty, calendars follow the ticker suffixes."""
    if not ctx.params.get("skip_closed_days", True):
        return None
    if universe_trades_on(universe, ctx.fire.as_of, asset_classes):
        return None
    return JobOutcome("skipped", {"reason": "market_closed", "as_of": ctx.fire.as_of.isoformat()})


# ---- building specs from config ----------------------------------------------------


def build_trigger(
    cfg: SessionTriggerConfig | DailyTriggerConfig | IntervalTriggerConfig,
) -> Trigger:
    if isinstance(cfg, SessionTriggerConfig):
        return SessionTrigger(
            cfg.calendar, anchor=cfg.anchor, offset=timedelta(minutes=cfg.offset_minutes)
        )
    if isinstance(cfg, DailyTriggerConfig):
        return DailyTrigger(
            cfg.at,
            tz=cfg.timezone,
            weekdays=frozenset(cfg.weekdays) if cfg.weekdays is not None else None,
            calendar=cfg.calendar,
        )
    return IntervalTrigger(timedelta(minutes=cfg.every_minutes))


def build_job_specs(
    config: SchedulerConfig,
    env: Mapping[str, str] | None = None,
    *,
    actions: Iterable[str] | None = None,
) -> list[JobSpec]:
    """Enabled jobs as specs. Fails fast on an unknown calendar, or on an
    action outside ``actions`` (the executor's, when given)."""
    env = os.environ if env is None else env
    known = set(actions) if actions is not None else None
    specs = []
    for job in config.jobs:
        if not job.enabled:
            continue
        if known is not None and job.action not in known:
            raise UnknownActionError(
                f"job {job.name!r}: unknown action {job.action!r}; available: {sorted(known)}"
            )
        trigger = build_trigger(job.trigger)
        _validate_calendar(trigger)
        ping = job.ping_url.get_secret_value() if job.ping_url else None
        if ping is None and job.ping_url_env:
            ping = env.get(job.ping_url_env) or None
        specs.append(
            JobSpec(
                name=job.name,
                action=job.action,
                trigger=trigger,
                params=dict(job.params),
                deadline=timedelta(minutes=job.deadline_minutes) if job.deadline_minutes else None,
                catch_up=job.catch_up or config.catch_up,
                ping_url=ping,
            )
        )
    return specs


def _validate_calendar(trigger: Trigger) -> None:
    from stonks.scheduling.calendar import get_calendar

    name = getattr(trigger, "calendar", None)
    if name:
        get_calendar(name)
