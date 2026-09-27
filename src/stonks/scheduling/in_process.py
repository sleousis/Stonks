"""The ``in_process`` backend: the scheduler inside ``stonks serve``.

For single-process installs. Jobs go through the API process's own app
services and :class:`~stonks.app.jobs.JobRunner`, so they share its lanes:
an ingest waits behind (never beside) other lake writers, and a tick
never runs next to another tick. The API lifespan starts it with
:func:`start_in_process_scheduler` and stops it with
:meth:`SchedulerHandle.stop`.

The actions mirror the ``api`` backend and read their results the same
way (:func:`~stonks.scheduling.api_backend.tick_job_outcome` and friends).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from stonks.logging import get_logger
from stonks.scheduling.api_backend import (
    TERMINAL_JOB_STATUSES,
    JobWaitTimeoutError,
    backup_job_outcome,
    calendar_job_outcome,
    calendar_refresh_body,
    ensure_body,
    ensure_step,
    health_view_outcome,
    ingest_job_outcome,
    ingest_window,
    retrain_job_outcome,
    tick_job_outcome,
)
from stonks.scheduling.config import SchedulerConfig, scheduler_config_from
from stonks.scheduling.jobs import (
    SCHEDULER_ACTOR,
    ActionRegistry,
    JobExecutor,
    JobOutcome,
    RunContext,
    borrow_markets,
    build_job_specs,
    closed_day_outcome,
    job_is_scoped,
    job_universe,
    no_gateways,
    retrain_body,
    universes_outcome,
)

IN_PROCESS_ACTIONS = ActionRegistry("in_process")

_log = get_logger("stonks.scheduling.in_process")


class InProcessExecutor(JobExecutor):
    backend = "in_process"

    def __init__(self, services: Any, *, timeout_seconds: float = 3 * 3600) -> None:
        #: ``stonks.app.services.Services`` of the running API.
        self.services = services
        self._timeout = timeout_seconds

    def actions(self) -> set[str]:
        return set(IN_PROCESS_ACTIONS.names())

    def execute(self, ctx: RunContext) -> JobOutcome:
        return IN_PROCESS_ACTIONS.get(ctx.spec.action)(ctx)

    def run_job(self, job: Any, kind: str, result_model: Any) -> tuple[str, str | None, Any, str]:
        """Wait for a submitted job; its result as a plain dict when it succeeded."""
        final = self.services.runner.wait(job.id, timeout=self._timeout)
        if final.status not in TERMINAL_JOB_STATUSES:
            raise JobWaitTimeoutError(f"job {job.id} still {final.status} after timeout")
        result = None
        if final.status == "succeeded":
            result = self.services.jobs.typed_result(job.id, kind, result_model).model_dump(
                mode="json"
            )
        return final.status, final.error, result, job.id

    def members(self, universe_id: str, day: Any) -> list[str]:
        """A stored universe's members on ``day`` (``job_universe`` resolver)."""
        return self.services.universes.members(universe_id, day).tickers

    def asset_classes(self, universe: list[str]) -> dict[str, str]:
        with self.services.context.lake() as lake:
            return lake.get_asset_classes(universe)


def _executor(ctx: RunContext) -> InProcessExecutor:
    executor = ctx.executor
    if not isinstance(executor, InProcessExecutor):  # pragma: no cover - wiring error
        raise TypeError("in_process actions need an InProcessExecutor")
    return executor


@IN_PROCESS_ACTIONS.register("ingest_prices")
def in_process_ingest_prices(ctx: RunContext) -> JobOutcome:
    from stonks.app.ingest import INGEST_JOB, IngestRequest, IngestResultView

    ex = _executor(ctx)
    universe = job_universe(ctx, ex.members)
    if not universe:
        return JobOutcome("skipped", {"reason": "empty_universe"})
    closed = closed_day_outcome(ctx, universe, ex.asset_classes(universe))
    if closed is not None:
        return closed
    since, until = ingest_window(ctx)
    job = ex.services.ingest.submit(
        IngestRequest(
            kind="prices",
            source=str(ctx.params.get("source", "eodhd")),
            tickers=universe,
            since=since,
            until=until,
        )
    )
    return ingest_job_outcome(*ex.run_job(job, INGEST_JOB, IngestResultView))


@IN_PROCESS_ACTIONS.register("ingest_metadata")
def in_process_ingest_metadata(ctx: RunContext) -> JobOutcome:
    from stonks.app.ingest import INGEST_JOB, IngestRequest, IngestResultView

    ex = _executor(ctx)
    universe = job_universe(ctx, ex.members)
    if not universe:
        return JobOutcome("skipped", {"reason": "empty_universe"})
    closed = closed_day_outcome(ctx, universe, ex.asset_classes(universe))
    if closed is not None:
        return closed
    job = ex.services.ingest.submit(
        IngestRequest.model_validate(
            {
                "kind": "metadata",
                "source": str(ctx.params.get("source", "eodhd")),
                "tickers": universe,
            }
        )
    )
    return ingest_job_outcome(*ex.run_job(job, INGEST_JOB, IngestResultView))


@IN_PROCESS_ACTIONS.register("ingest_borrow")
def in_process_ingest_borrow(ctx: RunContext) -> JobOutcome:
    """Like the ``api`` action, on the server's ``lake_write`` lane."""
    from stonks.app.ingest import INGEST_JOB, IngestRequest, IngestResultView

    markets = borrow_markets(ctx)
    if markets is None:
        return no_gateways()
    ex = _executor(ctx)
    job = ex.services.ingest.submit(IngestRequest(kind="borrow", markets=markets))
    return ingest_job_outcome(*ex.run_job(job, INGEST_JOB, IngestResultView))


@IN_PROCESS_ACTIONS.register("tick")
def in_process_tick(ctx: RunContext) -> JobOutcome:
    from stonks.app.ticks import TICK_JOB, TickRequest, TickResultView

    ex = _executor(ctx)
    universe = job_universe(ctx, ex.members)
    if not universe:
        return JobOutcome("skipped", {"reason": "empty_universe"})
    closed = closed_day_outcome(ctx, universe, ex.asset_classes(universe))
    if closed is not None:
        return closed
    job = ex.services.ticks.submit(
        TickRequest(
            as_of=ctx.fire.as_of,
            tickers=universe,
            scoped=job_is_scoped(ctx),
            bars_due_at=ctx.fire.scheduled_for,
            dry_run=bool(ctx.params.get("dry_run", False)),
        )
    )
    return tick_job_outcome(*ex.run_job(job, TICK_JOB, TickResultView))


@IN_PROCESS_ACTIONS.register("health")
def in_process_health(ctx: RunContext) -> JobOutcome:
    ex = _executor(ctx)
    view = ex.services.operations.run_health(
        job_universe(ctx, ex.members), actor="service:scheduler"
    )
    return health_view_outcome(ctx, view.model_dump(mode="json"))


@IN_PROCESS_ACTIONS.register("report")
def in_process_report(ctx: RunContext) -> JobOutcome:
    from stonks.scheduling.local import report_action

    return report_action(ctx)


# ---- hosting inside stonks serve --------------------------------------------------


@dataclass
class SchedulerHandle:
    scheduler: Any
    thread: threading.Thread
    lock: Any
    #: The notification delivery worker's thread (``None``: switched off).
    delivery: Any = None

    def stop(self, timeout: float = 30.0) -> None:
        """Ask the loop to stop, wait for the running job, release the lock."""
        self.scheduler.request_stop()
        if self.delivery is not None:
            self.delivery.stop(timeout=timeout)
        self.thread.join(timeout=timeout)
        self.lock.release()


def start_in_process_scheduler(
    services: Any,
    settings: Any,
    *,
    config: SchedulerConfig | None = None,
    notifier: Any = None,
) -> SchedulerHandle | None:
    """Start the scheduler on a daemon thread of the API process.

    Returns None (with a warning) when ``[scheduler].enabled`` is false or
    another scheduler already holds the lock (e.g. a separate worker), so
    the API starts either way.
    """
    from stonks.notify import notifier_from_settings
    from stonks.scheduling.deadman import DeadlineWatchdog, HttpPinger
    from stonks.scheduling.runs import RunStore
    from stonks.scheduling.scheduler import (
        InstanceLock,
        Scheduler,
        SchedulerAlreadyRunningError,
        default_lock_path,
    )

    config = config or scheduler_config_from(settings)
    if not config.enabled:
        return None
    executor = InProcessExecutor(services, timeout_seconds=config.job_timeout_minutes * 60)
    specs = build_job_specs(config, actions=executor.actions())
    lock = InstanceLock(default_lock_path(config, settings.state.path))
    try:
        lock.acquire()
    except SchedulerAlreadyRunningError as exc:
        _log.warning("scheduler.in_process_not_started", reason=str(exc))
        return None
    store = RunStore(settings.state.path)
    store.migrate()
    notifier = notifier or notifier_from_settings(settings)
    scheduler = Scheduler(
        specs,
        store,
        settings=settings,
        notifier=notifier,
        config=config,
        pinger=HttpPinger(timeout_seconds=config.ping_timeout_seconds),
        executor=executor,
    )
    thread = threading.Thread(
        target=scheduler.run_forever,
        kwargs={"watchdog": DeadlineWatchdog(specs, store, notifier)},
        name="stonks-scheduler",
        daemon=True,
    )
    thread.start()
    from stonks.scheduling.delivery import start_delivery_worker

    return SchedulerHandle(scheduler, thread, lock, delivery=start_delivery_worker(settings))


@IN_PROCESS_ACTIONS.register("backup")
def in_process_backup(ctx: RunContext) -> JobOutcome:
    """On the server's ``lake_write`` lane, through its own lake connection."""
    from stonks.app.backups import BACKUP_JOB, BackupResultView

    ex = _executor(ctx)
    job = ex.services.backups.submit()
    return backup_job_outcome(*ex.run_job(job, BACKUP_JOB, BackupResultView))


@IN_PROCESS_ACTIONS.register("price_alerts")
def in_process_price_alerts(ctx: RunContext) -> JobOutcome:
    """Every person's price alert rules against the latest closes."""
    out = _executor(ctx).services.price_alerts.evaluate(as_of=ctx.fire.as_of)
    return JobOutcome("succeeded", out.as_dict())


@IN_PROCESS_ACTIONS.register("model_retrain")
def in_process_model_retrain(ctx: RunContext) -> JobOutcome:
    """Refit on the server's JobRunner; each fit becomes a candidate version."""
    from stonks.app.model_versions import RETRAIN_JOB, RetrainRequest, RetrainResultView

    ex = _executor(ctx)
    job = ex.services.model_versions.submit_retrain(
        RetrainRequest.model_validate(retrain_body(ctx)), actor=SCHEDULER_ACTOR
    )
    return retrain_job_outcome(*ex.run_job(job, RETRAIN_JOB, RetrainResultView))


@IN_PROCESS_ACTIONS.register("connections_sync")
def in_process_connections_sync(ctx: RunContext) -> JobOutcome:
    from stonks.scheduling.local import connections_sync_action

    return connections_sync_action(ctx)


@IN_PROCESS_ACTIONS.register("universes_refresh")
def in_process_universes_refresh(ctx: RunContext) -> JobOutcome:
    """Like the ``api`` action, on the server's ``lake_write`` lane."""
    from stonks.app.universes import (
        UNIVERSE_ENSURE_JOB,
        UNIVERSE_REFRESH_JOB,
        EnsureDataRequest,
        UniverseRefreshView,
    )
    from stonks.ingest.ensure import EnsureReport

    ex = _executor(ctx)
    service = ex.services.universes
    results: dict[str, dict[str, Any]] = {}
    for universe in service.list():
        job = service.submit_refresh(universe.id)
        status, error, result, _ = ex.run_job(job, UNIVERSE_REFRESH_JOB, UniverseRefreshView)
        step: dict[str, Any] = {"refresh": status}
        if error:
            step["refresh_error"] = error
        if result is not None:
            step["members"] = result.get("members")
        if status == "succeeded" and ctx.params.get("ensure", True):
            request = EnsureDataRequest.model_validate(ensure_body(ctx))
            e_job = service.submit_ensure(universe.id, request)
            e_status, e_error, e_result, _ = ex.run_job(e_job, UNIVERSE_ENSURE_JOB, EnsureReport)
            step |= ensure_step(e_status, e_error, e_result)
        results[universe.id] = step
    return universes_outcome(results)


@IN_PROCESS_ACTIONS.register("broker_health")
def in_process_broker_health(ctx: RunContext) -> JobOutcome:
    from stonks.scheduling.local import broker_health_action

    return broker_health_action(ctx)


@IN_PROCESS_ACTIONS.register("live_reconcile")
def in_process_live_reconcile(ctx: RunContext) -> JobOutcome:
    """State DB and the gateway only: runs in this process."""
    from stonks.scheduling.local import live_reconcile_action

    return live_reconcile_action(ctx)


@IN_PROCESS_ACTIONS.register("ibkr_reauth_reminder")
def in_process_ibkr_reauth_reminder(ctx: RunContext) -> JobOutcome:
    from stonks.scheduling.local import ibkr_reauth_reminder_action

    return ibkr_reauth_reminder_action(ctx)


@IN_PROCESS_ACTIONS.register("calendars_refresh")
def in_process_calendars_refresh(ctx: RunContext) -> JobOutcome:
    """Like the ``api`` action, on the server's ``lake_write`` lane."""
    from stonks.app.calendars import (
        CALENDAR_REFRESH_JOB,
        CalendarRefreshRequest,
        CalendarRefreshView,
    )

    ex = _executor(ctx)
    job = ex.services.calendars.submit_refresh(
        CalendarRefreshRequest.model_validate(calendar_refresh_body(ctx))
    )
    status, error, result, job_id = ex.run_job(job, CALENDAR_REFRESH_JOB, CalendarRefreshView)
    return calendar_job_outcome(status, error, result, job_id)


@IN_PROCESS_ACTIONS.register("live_submit")
def in_process_live_submit(ctx: RunContext) -> JobOutcome:
    """Order tickets live in the state DB only, so every backend sends them
    the same way (the lake is not needed)."""
    from stonks.scheduling.local import live_submit_action

    return live_submit_action(ctx)


@IN_PROCESS_ACTIONS.register("live_stops")
def in_process_live_stops(ctx: RunContext) -> JobOutcome:
    """Protective stops live in the state DB and at the broker, so every
    backend syncs them the same way (the lake only for the ATR, read only)."""
    from stonks.scheduling.local import live_stops_action

    return live_stops_action(ctx)


@IN_PROCESS_ACTIONS.register("live_gate_days")
def in_process_live_gate_days(ctx: RunContext) -> JobOutcome:
    """Gate metrics read and write the state DB only, so every backend
    records them the same way (the lake is not needed)."""
    from stonks.scheduling.local import live_gate_days_action

    return live_gate_days_action(ctx)


@IN_PROCESS_ACTIONS.register("engine_start")
def in_process_engine_start_action(ctx: RunContext) -> JobOutcome:
    """The engine is its own process, controlled through files next to the
    state DB, so every backend starts it the same way (roadmap 21.2.5)."""
    from stonks.scheduling.jobs import engine_start_job

    return engine_start_job(ctx)


@IN_PROCESS_ACTIONS.register("engine_stop")
def in_process_engine_stop_action(ctx: RunContext) -> JobOutcome:
    """Every backend stops the engine the same way (roadmap 21.2.5)."""
    from stonks.scheduling.jobs import engine_stop_job

    return engine_stop_job(ctx)
