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

import os
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from stonks.accounts import AuditLog, Scope
from stonks.app.context import AppContext
from stonks.app.errors import ConflictError, NotFoundError, ValidationError
from stonks.logging import get_logger
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


class ScheduledJobView(BaseModel):
    name: str
    action: str
    trigger: str
    next_run_at: datetime | None
    next_as_of: date | None


class ScheduledRunView(BaseModel):
    id: str
    job_name: str
    action: str
    run_key: str
    scheduled_for: datetime
    as_of: str | None
    status: str
    catch_up: bool
    started_at: datetime
    finished_at: datetime | None
    detail: dict[str, Any] | None
    error: str | None

    @classmethod
    def of(cls, r: RunRecord) -> ScheduledRunView:
        return cls(**{f: getattr(r, f) for f in cls.model_fields})


class ScheduleView(BaseModel):
    #: The backend ``[scheduler].backend`` resolves to.
    backend: str
    #: True while this API process hosts the scheduler loop.
    hosted: bool
    jobs: list[ScheduledJobView]
    recent: list[ScheduledRunView]


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
                    next_run_at=fire.scheduled_for if fire else None,
                    next_as_of=fire.as_of if fire else None,
                )
            )
        recent = self._store().recent(limit=min(limit, RECENT_RUNS_MAX))
        return ScheduleView(
            backend=resolve_backend(self.config, os.environ),
            hosted=self.handle is not None,
            jobs=jobs,
            recent=[ScheduledRunView.of(r) for r in recent],
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
        return metrics_text(settings.state.path, now=self._clock(), specs=specs, latest_bars=bars)

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
