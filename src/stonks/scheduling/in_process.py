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
    health_view_outcome,
    ingest_job_outcome,
    ingest_window,
    tick_job_outcome,
)
from stonks.scheduling.config import SchedulerConfig, scheduler_config_from
from stonks.scheduling.jobs import (
    ActionRegistry,
    JobExecutor,
    JobOutcome,
    RunContext,
    build_job_specs,
    closed_day_outcome,
    job_universe,
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
    universe = job_universe(ctx)
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


@IN_PROCESS_ACTIONS.register("tick")
def in_process_tick(ctx: RunContext) -> JobOutcome:
    from stonks.app.ticks import TICK_JOB, TickRequest, TickResultView

    ex = _executor(ctx)
    universe = job_universe(ctx)
    if not universe:
        return JobOutcome("skipped", {"reason": "empty_universe"})
    closed = closed_day_outcome(ctx, universe, ex.asset_classes(universe))
    if closed is not None:
        return closed
    job = ex.services.ticks.submit(
        TickRequest(
            as_of=ctx.fire.as_of,
            tickers=universe,
            dry_run=bool(ctx.params.get("dry_run", False)),
        )
    )
    return tick_job_outcome(*ex.run_job(job, TICK_JOB, TickResultView))


@IN_PROCESS_ACTIONS.register("health")
def in_process_health(ctx: RunContext) -> JobOutcome:
    view = _executor(ctx).services.operations.health_report(job_universe(ctx))
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

    def stop(self, timeout: float = 30.0) -> None:
        """Ask the loop to stop, wait for the running job, release the lock."""
        self.scheduler.request_stop()
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
    return SchedulerHandle(scheduler, thread, lock)
