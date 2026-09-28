"""ScheduleService: the scheduler as the API sees it (roadmap 12.2-12.3).

- Hosts the in-process scheduler when ``[scheduler].backend`` resolves to
  ``in_process`` (:meth:`start_hosted` / :meth:`stop_hosted`, called by the
  API lifespan).
- Lists jobs with their next fire and the recent scheduled runs.
- Runs a job now (a ``manual:`` run key, audited), on a background thread
  through the API's own job runner, like the hosted scheduler does.
- Renders Prometheus metrics and the readiness / liveness probes.

Transport-neutral: probes come back as :class:`ProbeView` and metrics as
text; the API decides status codes and auth.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from stonks.accounts import AuditLog, Scope
from stonks.app.context import AppContext
from stonks.app.errors import ConflictError, NotFoundError, ValidationError
from stonks.logging import get_logger
from stonks.scheduling.calendar import US_CALENDAR, get_calendar
from stonks.scheduling.config import SchedulerConfig, resolve_backend, scheduler_config_from
from stonks.scheduling.in_process import (
    IN_PROCESS_ACTIONS,
    InProcessExecutor,
    SchedulerHandle,
    start_in_process_scheduler,
)
from stonks.scheduling.jobs import JobSpec, build_job_specs
from stonks.scheduling.metrics import (
    latest_daily_bars,
    liveness,
    metrics_text,
    readiness,
    scheduler_liveness,
)
from stonks.scheduling.runs import MANUAL_PREFIX, RunRecord, RunStore
from stonks.scheduling.triggers import Fire

_log = get_logger("stonks.app.schedule")

RECENT_RUNS_MAX = 200


class RunNowRequest(BaseModel):
    #: The date the run is for; default today (UTC).
    as_of: date | None = None


class RunNowView(BaseModel):
    job: str
    run_key: str
    as_of: date
    #: Always ``started``: the run continues in the background; watch
    #: ``GET /api/schedule`` for its status.
    status: str = "started"


#: Why a job has nothing to do on this install: the intraday engine is off,
#: live options are off, or no IB Gateway is set up. The job still fires and
#: skips; the console groups it apart and offers no Run now.
JobOffReason = Literal["engine_off", "options_off", "no_gateway"]


class ScheduledJobView(BaseModel):
    name: str
    action: str
    #: Short form, as the CLI prints it (``XNYS close + 45 min on trading days``).
    trigger: str
    #: When it runs in plain English, for people.
    trigger_text: str = ""
    next_run_at: datetime | None
    next_as_of: date | None
    off_reason: JobOffReason | None = None


#: Who started a run: a trigger, Run now, or someone outside the scheduler
#: (a trading run from the console's Orders page, the API or the CLI).
RunOrigin = Literal["schedule", "run_now", "outside"]


class ScheduledRunView(BaseModel):
    id: str
    job_name: str
    action: str
    run_key: str
    scheduled_for: datetime
    as_of: str | None
    #: ``running``, ``succeeded``, ``skipped`` or ``failed``; a trading run
    #: from outside the scheduler can also be ``partial``.
    status: str
    catch_up: bool
    started_at: datetime
    finished_at: datetime | None
    detail: dict[str, Any] | None
    error: str | None
    origin: RunOrigin = "schedule"

    @classmethod
    def of(cls, r: RunRecord) -> ScheduledRunView:
        fields = {f: getattr(r, f) for f in cls.model_fields if f != "origin"}
        origin: RunOrigin = "run_now" if r.run_key.startswith(MANUAL_PREFIX) else "schedule"
        return cls(**fields, origin=origin)


#: ``tick_runs.status`` in the scheduled runs' words.
_TICK_RUN_STATUS = {"ok": "succeeded", "partial": "partial", "error": "failed"}


def _utc_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def outside_trading_runs(state: Any, *, job_name: str, limit: int) -> list[ScheduledRunView]:
    """Trading runs no scheduled run started (the console, the API, the
    CLI), newest first, as schedule rows. Only the status, the day and the
    tick id: order and fill counts stay on the Trading runs page, which
    scopes them to the reader's portfolios."""
    from stonks.app.ticks import _AS_OF_SQL, _as_of

    rows = state.sql(
        f"SELECT t.*, {_AS_OF_SQL} FROM tick_runs t WHERE t.id NOT IN ("
        " SELECT json_extract(detail_json, '$.tick_id') FROM scheduled_runs"
        " WHERE action = 'tick' AND json_extract(detail_json, '$.tick_id') IS NOT NULL)"
        " AND NOT (t.status = 'running' AND EXISTS ("
        "  SELECT 1 FROM scheduled_runs WHERE action = 'tick' AND status = 'running'))"
        " ORDER BY t.started_at DESC, t.rowid DESC LIMIT ?",
        [limit],
    )
    views = []
    for row in rows:
        summary = json.loads(row["summary_json"]) if row["summary_json"] else {}
        started = _utc_datetime(row["started_at"])
        as_of = _as_of(row)
        detail: dict[str, Any] = {"tick_id": row["id"]}
        if "dry_run" in summary:
            detail["dry_run"] = bool(summary["dry_run"])
        views.append(
            ScheduledRunView(
                id=row["id"],
                job_name=job_name,
                action="tick",
                run_key=f"outside:{row['id']}",
                scheduled_for=started,
                as_of=as_of.isoformat() if as_of else None,
                status=_TICK_RUN_STATUS.get(row["status"], row["status"]),
                catch_up=False,
                started_at=started,
                finished_at=_utc_datetime(row["finished_at"]) if row["finished_at"] else None,
                detail=detail,
                error=summary.get("error"),
                origin="outside",
            )
        )
    return views


#: The console's "market opens soon" window starts this long before the open.
PRE_OPEN_MINUTES = 30


class SessionTimesView(BaseModel):
    #: The session's local trading date.
    date: date
    #: ``PRE_OPEN_MINUTES`` before the open (UTC).
    pre_open: datetime
    open: datetime
    close: datetime


class MarketSessionsView(BaseModel):
    #: The exchange calendar the scheduler's session triggers follow.
    calendar: str
    is_open: bool
    #: Today's session, or null when the market does not trade today.
    today: SessionTimesView | None
    #: The first session after today.
    next: SessionTimesView


class ScheduleView(BaseModel):
    #: The backend ``[scheduler].backend`` resolves to.
    backend: str
    #: True while this API process hosts the scheduler loop.
    hosted: bool
    #: A scheduler (here or on its own) heartbeated lately or is running a job.
    running: bool = False
    jobs: list[ScheduledJobView]
    recent: list[ScheduledRunView]
    #: Market session times from the calendar (null if it cannot be read).
    market: MarketSessionsView | None = None


class ProbeView(BaseModel):
    status: str
    #: Check name -> ``ok`` or ``fail`` (details are logged, not served).
    checks: dict[str, str] = Field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def failing(self) -> list[str]:
        return [k for k, v in self.checks.items() if v != "ok"]


def _probe(ok: bool, checks: dict[str, str], name: str) -> ProbeView:
    view = ProbeView(
        status="ok" if ok else "fail",
        checks={k: "ok" if v == "ok" else "fail" for k, v in checks.items()},
    )
    if not ok:
        _log.warning(f"probe.{name}_failed", checks=checks)
    return view


class ScheduleService:
    def __init__(
        self,
        context: AppContext,
        *,
        config: SchedulerConfig | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """``config`` defaults to ``settings.scheduler`` (once that field
        exists), else ``[scheduler]`` of the TOML config."""
        self._ctx = context
        self._config = config
        self._clock = clock or (lambda: datetime.now(UTC))
        self._services: Any = None
        self._inflight: set[str] = set()
        self._guard = threading.Lock()
        self.handle: SchedulerHandle | None = None

    def bind(self, services: Any) -> None:
        """The ``Services`` container jobs run through (set once at wiring)."""
        self._services = services

    @property
    def config(self) -> SchedulerConfig:
        if self._config is None:
            self._config = scheduler_config_from(self._ctx.settings)
        return self._config

    def _store(self) -> RunStore:
        return RunStore(self._ctx.settings.state.path)

    def _specs(self, *, runnable: bool = False) -> list[JobSpec]:
        actions = IN_PROCESS_ACTIONS.names() if runnable else None
        try:
            return build_job_specs(self.config, actions=actions)
        except ValueError as exc:  # unknown action or calendar in [scheduler]
            raise ValidationError(f"[scheduler] config: {exc}") from None

    # ---- hosting ------------------------------------------------------------------

    def start_hosted(self) -> SchedulerHandle | None:
        """Start the in-process scheduler when the backend resolves to
        ``in_process`` (None otherwise, or when another holds the lock)."""
        if self.handle is not None:
            return self.handle
        if resolve_backend(self.config, os.environ) != "in_process":
            return None
        self.handle = start_in_process_scheduler(
            self._services, self._ctx.settings, config=self.config
        )
        if self.handle is not None:
            _log.info("scheduler.hosted")
        return self.handle

    def stop_hosted(self, timeout: float = 30.0) -> None:
        handle, self.handle = self.handle, None
        if handle is not None:
            handle.stop(timeout=timeout)
            _log.info("scheduler.hosted_stopped")

    # ---- reads ----------------------------------------------------------------------

    def overview(self, *, limit: int = 50) -> ScheduleView:
        now = self._clock()
        jobs = []
        for spec in self._specs():
            fire = spec.trigger.next_fire(now)
            jobs.append(
                ScheduledJobView(
                    name=spec.name,
                    action=spec.action,
                    trigger=spec.trigger.describe(),
                    trigger_text=spec.trigger.plain(),
                    next_run_at=fire.scheduled_for if fire else None,
                    next_as_of=fire.as_of if fire else None,
                    off_reason=self._off_reason(spec.action),
                )
            )
        limit = min(limit, RECENT_RUNS_MAX)
        store = self._store()
        recent = [ScheduledRunView.of(r) for r in store.recent(limit=limit)]
        recent = sorted(
            recent + self._outside_runs(jobs, limit), key=lambda r: r.started_at, reverse=True
        )[:limit]
        return ScheduleView(
            backend=resolve_backend(self.config, os.environ),
            hosted=self.handle is not None,
            running=self._scheduler_running(store, recent, now),
            jobs=jobs,
            recent=recent,
            market=self.market_sessions(now),
        )

    def _off_reason(self, action: str) -> JobOffReason | None:
        settings = self._ctx.settings
        if action in ("engine_start", "engine_stop") and not settings.engine.enabled:
            return "engine_off"
        if action == "options_live" and not settings.production.options.live:
            return "options_off"
        if (
            action in ("broker_health", "ibkr_reauth_reminder", "ingest_borrow")
            and not settings.brokers.ibkr.gateways
        ):
            return "no_gateway"
        return None

    def _outside_runs(self, jobs: list[ScheduledJobView], limit: int) -> list[ScheduledRunView]:
        """Trading runs started outside the scheduler, named after the
        trading run job so its Last run counts them too."""
        job_name = next((j.name for j in jobs if j.action == "tick"), "tick")
        try:
            with self._ctx.state() as state:
                return outside_trading_runs(state, job_name=job_name, limit=limit)
        except Exception as exc:  # the schedule still renders without them
            _log.warning("schedule.outside_runs_failed", error_type=type(exc).__name__)
            return []

    def _scheduler_running(
        self, store: RunStore, recent: list[ScheduledRunView], now: datetime
    ) -> bool:
        """A fresh heartbeat, or a scheduled run in progress (the heartbeat
        is written between jobs, so a long trading run is not a silence)."""
        if any(r.status == "running" and r.origin != "outside" for r in recent):
            return True
        silence = max(timedelta(minutes=5), timedelta(seconds=3 * self.config.watchdog_seconds))
        try:
            return scheduler_liveness(store, now=now, max_silence=silence).ok
        except Exception as exc:
            _log.warning("schedule.liveness_failed", error_type=type(exc).__name__)
            return False

    def market_sessions(self, now: datetime | None = None) -> MarketSessionsView | None:
        """Pre-open, open and close for today and the next session, from
        the calendar of the first session trigger (``XNYS`` by default)."""
        now = now or self._clock()
        name = next(
            (
                cal
                for job in self.config.jobs
                if (cal := getattr(job.trigger, "calendar", None)) and job.trigger.type == "session"
            ),
            US_CALENDAR,
        )
        try:
            calendar = get_calendar(name)
            day = now.astimezone(UTC).date()
            today = calendar.session(day)
            upcoming = calendar.next_session(day)
        except Exception as exc:  # outside the calendar's range, unknown name
            _log.warning("schedule.market_sessions_failed", calendar=name, error=str(exc))
            return None

        def times(s: Any) -> SessionTimesView:
            return SessionTimesView(
                date=s.date,
                pre_open=s.open - timedelta(minutes=PRE_OPEN_MINUTES),
                open=s.open,
                close=s.close,
            )

        return MarketSessionsView(
            calendar=name,
            is_open=today is not None and today.open <= now < today.close,
            today=times(today) if today is not None else None,
            next=times(upcoming),
        )

    # ---- run now ---------------------------------------------------------------------

    def run_now(self, scope: Scope, job: str, request: RunNowRequest) -> RunNowView:
        """Start ``job`` now on a background thread (the tick and ingest
        wait for their lanes in the job runner). Audited; one manual run of a
        job at a time."""
        spec = next((s for s in self._specs(runnable=True) if s.name == job), None)
        if spec is None:
            raise NotFoundError(f"no scheduled job {job!r}")
        now = self._clock()
        as_of = request.as_of or now.date()
        fire = Fire(now, as_of, f"{MANUAL_PREFIX}{now.isoformat()}")
        with self._guard:
            if job in self._inflight:
                raise ConflictError(f"a manual run of {job!r} is already in progress")
            self._inflight.add(job)
        try:
            with self._ctx.state() as state:
                AuditLog(state).record(
                    scope.actor,
                    "schedule.run_now",
                    "scheduled_job",
                    job,
                    details={"as_of": as_of.isoformat(), "run_key": fire.key},
                )
            thread = threading.Thread(
                target=self._run_manual,
                args=(spec, fire),
                name=f"stonks-run-now-{job}",
                daemon=True,
            )
            thread.start()
        except BaseException:
            self._release(job)
            raise
        _log.info("schedule.run_now", job=job, run_key=fire.key, actor=scope.actor)
        return RunNowView(job=job, run_key=fire.key, as_of=as_of)

    def _run_manual(self, spec: JobSpec, fire: Fire) -> None:
        from stonks.notify import notifier_from_settings
        from stonks.scheduling.scheduler import Scheduler

        try:
            store = self._store()
            store.migrate()
            scheduler = Scheduler(
                [spec],
                store,
                settings=self._ctx.settings,
                notifier=notifier_from_settings(self._ctx.settings),
                config=self.config,
                executor=InProcessExecutor(
                    self._services, timeout_seconds=self.config.job_timeout_minutes * 60
                ),
            )
            scheduler.run_one(spec, fire)
        except Exception as exc:  # run_one records job failures; this is wiring
            _log.error("schedule.run_now_failed", job=spec.name, error_type=type(exc).__name__)
        finally:
            self._release(spec.name)

    def _release(self, job: str) -> None:
        with self._guard:
            self._inflight.discard(job)

    # ---- metrics and probes ------------------------------------------------------------

    def metrics(self) -> str:
        settings = self._ctx.settings
        try:
            specs = self._specs()
        except ValidationError:
            specs = []
        bars = None
        from stonks.production.universe import production_tickers

        if settings.production.universe:
            try:
                with self._ctx.lake() as lake:
                    universe = production_tickers(
                        lake, settings.production.universe, self._clock().date()
                    )
                    bars = latest_daily_bars(lake, universe)
            except Exception as exc:  # data age is optional; the rest still renders
                _log.warning("metrics.data_age_failed", error_type=type(exc).__name__)
        text = metrics_text(settings.state.path, now=self._clock(), specs=specs, latest_bars=bars)
        return text + self._lab_queue_metrics() + self._engine_metrics()

    def _engine_metrics(self) -> str:
        """Roadmap 21.3.4: stream and engine families from the engine's
        status row (empty with no engine or when it can't be read)."""
        from stonks.engine.status import engine_metrics_text

        monitor = self._ctx.settings.streaming.monitor
        try:
            return engine_metrics_text(
                self._ctx.settings.state.path,
                now=self._clock(),
                stale_after=timedelta(seconds=monitor.stale_after_seconds),
            )
        except Exception as exc:
            _log.warning("metrics.engine_failed", error_type=type(exc).__name__)
            return ""

    def _lab_queue_metrics(self) -> str:
        """Roadmap 14.9: the lab worker queue (empty when it can't be read)."""
        from stonks.lab.offload.health import metrics_text as lab_queue_metrics

        try:
            with self._ctx.state() as state:
                return lab_queue_metrics(
                    state,
                    now=self._clock(),
                    lease_seconds=self._ctx.settings.lab.offload.lease_seconds,
                )
        except Exception as exc:
            _log.warning("metrics.lab_queue_failed", error_type=type(exc).__name__)
            return ""

    def readiness(self) -> ProbeView:
        probe = readiness(self._ctx.settings.state.path, self._ctx.settings.lake.path)
        return _probe(probe.ok, probe.checks, "readiness")

    def liveness(self) -> ProbeView:
        checks = {"process": "ok"}
        ok = liveness().ok
        handle = self.handle
        if handle is not None:
            silence = max(timedelta(minutes=5), timedelta(seconds=3 * self.config.watchdog_seconds))
            sched = scheduler_liveness(self._store(), now=self._clock(), max_silence=silence)
            alive = sched.ok and handle.thread.is_alive()
            checks["scheduler"] = "ok" if alive else sched.checks.get("scheduler", "fail")
            ok = ok and alive
        return _probe(ok, checks, "liveness")
