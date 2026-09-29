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
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from stonks.logging import get_logger
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

_log = get_logger("stonks.scheduling.jobs")

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

    def recover(self, settings: Any) -> None:  # noqa: B027 - optional hook
        """Called once when the scheduler starts, holding its instance lock:
        clean up after an executor of this kind that died mid-run."""


# ---- helpers shared by the backends ------------------------------------------------


#: ``(universe_id, day) -> members``: how a backend reads a stored
#: universe's members (the API, the app services, or the lake).
MembersResolver = Callable[[str, date], list[str]]


def job_is_scoped(ctx: RunContext) -> bool:
    """A job with its own ``params.tickers`` (e.g. a crypto-only tick)
    covers only those tickers: its tick must leave other holdings alone."""
    return bool(ctx.params.get("tickers"))


def job_universe(ctx: RunContext, members: MembersResolver | None = None) -> list[str]:
    """``params.tickers``, else ``[production].universe``. A universe id
    there is resolved on the fire's date through ``members``; without a
    resolver, or when it fails (the universe was never refreshed), the
    job has no tickers and skips."""
    tickers = ctx.params.get("tickers")
    if tickers:
        return [str(t) for t in tickers]
    configured = ctx.settings.production.universe
    if not isinstance(configured, str):
        return list(configured)
    if members is None:
        return []
    try:
        return list(members(configured, ctx.fire.as_of))
    except Exception as exc:  # a missing universe means nothing to trade today
        _log.warning(
            "scheduler.universe_unresolved",
            universe_id=configured,
            error=f"{type(exc).__name__}: {exc}",
        )
        return []


def ensure_window(ctx: RunContext) -> tuple[date, date]:
    """The trailing window the ``universes_refresh`` job fills
    (``params.ensure_days``, default 10) up to the fire's date."""
    days = int(ctx.params.get("ensure_days", 10))
    return ctx.fire.as_of - timedelta(days=days), ctx.fire.as_of


def no_gateways() -> JobOutcome:
    """The outcome of a job that needs an IB Gateway while none is set."""
    return JobOutcome("skipped", {"reason": "no_gateways"})


def borrow_markets(ctx: RunContext) -> list[str] | None:
    """The markets the ``ingest_borrow`` job pulls (``params.markets``, else
    ``[sources.ibkr_borrow] markets``), or ``None`` while no IB Gateway is
    configured: only a book at IBKR reads borrow rates (roadmap 19.14)."""
    from stonks.ingest.sources.ibkr_borrow import resolve_markets

    if not ctx.settings.brokers.ibkr.gateways:
        return None
    raw = ctx.params.get("markets")
    wanted = [raw] if isinstance(raw, str) else list(raw or [])
    return resolve_markets(wanted, ctx.settings.sources.ibkr_borrow.markets)


def universes_outcome(results: Mapping[str, Mapping[str, Any]]) -> JobOutcome:
    """The ``universes_refresh`` outcome from each universe's step results
    (``refresh`` / ``ensure`` statuses): failed when any step failed."""
    if not results:
        return JobOutcome("skipped", {"reason": "no_universes"})
    failed = sorted(
        uid
        for uid, r in results.items()
        if r.get("refresh") != "succeeded" or r.get("ensure", "succeeded") != "succeeded"
    )
    detail = {"universes": dict(results), "failed": failed}
    return JobOutcome("failed" if failed else "succeeded", detail)


def retrain_body(ctx: RunContext) -> dict[str, Any]:
    """The ``model_retrain`` request (``app.model_versions.RetrainRequest``):
    the fire's date, plus ``params.strategy_ids``, ``force`` and ``tickers``."""
    body: dict[str, Any] = {"as_of": ctx.fire.as_of.isoformat()}
    for key in ("strategy_ids", "tickers"):
        if ctx.params.get(key):
            body[key] = [str(v) for v in ctx.params[key]]
    if ctx.params.get("force"):
        body["force"] = True
    return body


def verify_body(ctx: RunContext) -> dict[str, Any]:
    """The ``lab_verify`` request (``app.lab_verify.VerifyRequest``): alert
    on a move, plus ``params.targets`` and ``tolerance``."""
    body: dict[str, Any] = {"alert": True}
    if ctx.params.get("targets"):
        body["targets"] = [str(v) for v in ctx.params["targets"]]
    if ctx.params.get("tolerance") is not None:
        body["tolerance"] = float(ctx.params["tolerance"])
    return body


def verify_outcome(result: Mapping[str, Any], job_id: str | None = None) -> JobOutcome:
    """A verify result (``VerifyResultView`` as JSON): failed when a result
    moved (the alert went out), skipped when nothing was checked."""
    detail = {k: result.get(k) for k in ("checked", "moved", "alerted") if k in result}
    if job_id is not None:
        detail["job_id"] = job_id
    if not result.get("checked"):
        return JobOutcome("skipped", {**detail, "reason": "nothing_to_verify"})
    if result.get("moved"):
        return JobOutcome("failed", detail)
    return JobOutcome("succeeded", detail)


def retrain_outcome(result: Mapping[str, Any], job_id: str | None = None) -> JobOutcome:
    """A retrain result (``RetrainResultView`` as JSON): failed when a fit
    failed, skipped when nothing needed a refit."""
    detail = {k: result.get(k) for k in ("as_of", "candidates", "failed", "skipped") if k in result}
    if job_id is not None:
        detail["job_id"] = job_id
    if result.get("failed"):
        return JobOutcome("failed", detail)
    if not result.get("candidates"):
        return JobOutcome("skipped", {**detail, "reason": "nothing_to_retrain"})
    return JobOutcome("succeeded", detail)


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


# ---- the intraday engine (roadmap 21.2.5) ------------------------------------------


def engine_launcher() -> Any:
    """The launcher ``engine_start`` uses (tests swap it for a fake)."""
    from stonks.engine.control import SubprocessLauncher

    return SubprocessLauncher()


def _engine_control(ctx: RunContext) -> Any:
    from stonks.engine.control import EngineControl
    from stonks.engine.settings import control_dir_for

    return EngineControl(control_dir_for(ctx.settings.engine, ctx.settings.state.path))


def engine_start_job(ctx: RunContext) -> JobOutcome:
    """Start the engine process for the fire's session, before the open.
    Skips while ``[engine]`` is off or has no books, on a closed day, and
    when an engine already runs. The engine talks to no API, so every
    backend starts it the same way (a detached ``python -m stonks.engine
    run``)."""
    from stonks.engine.process import session_window

    cfg = ctx.settings.engine
    session = ctx.fire.as_of
    if not cfg.enabled:
        return JobOutcome("skipped", {"reason": "engine_off"})
    if not cfg.books:
        return JobOutcome("skipped", {"reason": "no_books"})
    if session_window(cfg.calendar, session) is None:
        return JobOutcome("skipped", {"reason": "market_closed", "as_of": session.isoformat()})
    control = _engine_control(ctx)
    if control.running():
        return JobOutcome("skipped", {"reason": "already_running"})
    result = engine_launcher().launch(control, session)
    return JobOutcome("succeeded", {"pid": result.pid, "session": session.isoformat()})


def engine_stop_job(ctx: RunContext) -> JobOutcome:
    """Ask the running engine to stop after the close and wait for it
    (``[engine] stop_timeout_seconds``). The engine stops itself at the same
    time, so this is the backstop. It runs even with ``[engine]`` off, so an
    engine left running is still stopped."""
    control = _engine_control(ctx)
    if not control.running():
        reason = "not_running" if ctx.settings.engine.enabled else "engine_off"
        return JobOutcome("skipped", {"reason": reason})
    control.request_stop(f"scheduler job {ctx.spec.name}")
    timeout = ctx.settings.engine.stop_timeout_seconds
    if control.wait_stopped(timeout):
        return JobOutcome("succeeded", {"stopped": True})
    return JobOutcome("failed", {"stopped": False, "waited_seconds": timeout})


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


def price_source(ctx: RunContext) -> str:
    """The data source of a scheduled price update: the job's ``source``
    param, else the configured default, or Yahoo while the default has no
    key (:func:`stonks.ingest.sources.registry.scheduled_source_id`)."""
    from stonks.ingest.sources.registry import scheduled_source_id

    return str(ctx.params.get("source") or scheduled_source_id(ctx.settings.sources))


def briefing_outcome(view: dict[str, Any]) -> JobOutcome:
    """A briefings run as a job outcome: skipped while briefings are off."""
    keys = ("kind", "as_of", "skipped", "people", "sent", "failed")
    detail = {k: view.get(k) for k in keys}
    if view.get("skipped"):
        return JobOutcome("skipped", {"reason": view["skipped"], **detail})
    return JobOutcome("succeeded", detail)
