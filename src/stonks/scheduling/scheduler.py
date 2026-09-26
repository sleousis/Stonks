"""The built-in scheduler (roadmap 12.2).

One process runs every enabled :class:`JobSpec` on its trigger:

- **Single instance.** :class:`InstanceLock` (an OS file lock via
  ``filelock``, released by the OS if the process dies) keeps a second
  scheduler from starting. Independently, every fire is *claimed* by an
  ``INSERT OR IGNORE`` on ``scheduled_runs (job_name, run_key)``, so a fire
  can run at most once even if two schedulers did run, or a manual
  catch-up raced the loop.
- **Catch-up on start.** Fires missed while the scheduler was down (after
  the job's last recorded run, within ``catch_up_window_hours``; a job
  that never ran has nothing to catch up) are
  handled per the job's policy: ``none`` drops them, ``latest`` runs only
  the most recent, ``all`` runs up to ``max_catch_up_runs`` of the most
  recent, oldest first. Fires that already have a run row are never rerun.
- **Coalescing.** While running, a job with several fires due at one
  wake-up (the machine slept, a long job held the loop) runs once, for the
  latest fire. Missed-run storms can't happen.
- **Sequential execution.** Jobs run one at a time in fire order on the
  scheduler thread (the lake has a single writer; the tick must follow the
  ingest). A job is never interrupted: shutdown waits for the running job,
  then stops before starting the next one.
- **Injected time.** ``clock`` and ``wait`` are parameters, so tests drive
  the loop without sleeping.

Every run records ``actor = service:scheduler``; the watchdog thread
(:class:`~stonks.scheduling.deadman.DeadlineWatchdog`) and the heartbeat
share the loop's store.
"""

from __future__ import annotations

import os
import socket
import threading
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from stonks.logging import get_logger
from stonks.notify import Notification, Notifier
from stonks.scheduling.config import SchedulerConfig
from stonks.scheduling.deadman import DeadlineWatchdog, NullPinger, Pinger
from stonks.scheduling.jobs import JobExecutor, JobOutcome, JobSpec, RunContext
from stonks.scheduling.runs import RunStore
from stonks.scheduling.triggers import Fire

_log = get_logger("stonks.scheduling.scheduler")


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class SchedulerAlreadyRunningError(RuntimeError):
    """Another scheduler holds the single-instance lock."""


class InstanceLock:
    """An exclusive, non-blocking OS file lock around ``filelock``."""

    def __init__(self, path: str | Path) -> None:
        from filelock import FileLock

        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = FileLock(str(self.path))

    def acquire(self) -> None:
        from filelock import Timeout

        try:
            self._lock.acquire(timeout=0)
        except Timeout:
            raise SchedulerAlreadyRunningError(
                f"another scheduler holds {self.path}; refusing to start a second one"
            ) from None

    def release(self) -> None:
        if self._lock.is_locked:
            self._lock.release()

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


@dataclass(frozen=True)
class DueRun:
    spec: JobSpec
    fire: Fire
    catch_up: bool


@dataclass(frozen=True)
class RunResult:
    job_name: str
    run_id: str | None  # None when the fire was already claimed
    fire: Fire
    status: str  # succeeded | skipped | failed | already_claimed
    catch_up: bool
    detail: dict[str, Any]


@dataclass(frozen=True)
class NextRun:
    job_name: str
    action: str
    trigger: str
    next_fire: Fire | None


class Scheduler:
    def __init__(
        self,
        specs: Sequence[JobSpec],
        store: RunStore,
        *,
        settings: Any,
        notifier: Notifier,
        config: SchedulerConfig | None = None,
        pinger: Pinger | None = None,
        clock: Clock | None = None,
        instance_id: str | None = None,
        executor: JobExecutor | None = None,
    ) -> None:
        self.specs = list(specs)
        self.store = store
        self.settings = settings
        self.notifier = notifier
        self.config = config or SchedulerConfig()
        self.pinger = pinger or NullPinger()
        self.clock = clock or SystemClock()
        self.instance_id = instance_id or f"sched_{uuid.uuid4().hex[:12]}"
        self._cursors: dict[str, datetime] = {}
        self._startup: list[DueRun] = []
        self._started = False
        self._stop = threading.Event()
        self.pending = 0  # due runs not yet started (queue depth)
        if executor is None:
            from stonks.scheduling.local import LocalExecutor

            executor = LocalExecutor()
        self.executor = executor
        executor.bind_stop(self._stop)

    # ---- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Recover interrupted rows, register this instance and plan the
        startup catch-up. Call while holding the :class:`InstanceLock`."""
        now = self.clock.now()
        recovered = self.store.recover_interrupted(now=now)
        if recovered:
            _log.warning("scheduler.recovered_interrupted", runs=recovered)
        self.store.register_instance(
            self.instance_id, host=socket.gethostname(), pid=os.getpid(), now=now
        )
        self._startup = self._plan_catch_up(now)
        self._cursors = {spec.name: now for spec in self.specs}
        self._started = True
        _log.info(
            "scheduler.started",
            instance_id=self.instance_id,
            jobs=[s.name for s in self.specs],
            catch_up=[f"{d.spec.name}:{d.fire.key}" for d in self._startup],
        )

    def request_stop(self) -> None:
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def stop(self) -> None:
        self.store.mark_stopped(self.instance_id, now=self.clock.now())
        self.executor.close()
        _log.info("scheduler.stopped", instance_id=self.instance_id)

    # ---- planning ----------------------------------------------------------

    def _plan_catch_up(self, now: datetime) -> list[DueRun]:
        window_start = now - timedelta(hours=self.config.catch_up_window_hours)
        out: list[DueRun] = []
        for spec in self.specs:
            if spec.catch_up == "none":
                continue
            last = self.store.last_scheduled_for(spec.name)
            if last is None:
                # Never ran: nothing was missed, so a fresh install or a
                # newly added job doesn't replay yesterday on first start.
                continue
            start = max(window_start, last)
            fires = spec.trigger.fires_between(start, now)
            if not fires:
                continue
            done = self.store.keys_with_runs(spec.name, [f.key for f in fires])
            if spec.catch_up == "latest":
                chosen = [fires[-1]] if fires[-1].key not in done else []
            else:
                todo = [f for f in fires if f.key not in done]
                chosen = todo[-self.config.max_catch_up_runs :]
            out.extend(DueRun(spec, f, catch_up=True) for f in chosen)
        return sorted(out, key=lambda d: (d.fire.scheduled_for, d.spec.name))

    def due(self, now: datetime) -> list[DueRun]:
        """What should run at ``now``: pending startup catch-ups plus, per
        job, the latest fire since the previous wake-up. Doesn't advance."""
        if not self._started:
            raise RuntimeError("call start() first")
        out = list(self._startup)
        planned = {(d.spec.name, d.fire.key) for d in out}
        for spec in self.specs:
            fires = spec.trigger.fires_between(self._cursors[spec.name], now)
            if fires and (spec.name, fires[-1].key) not in planned:
                if len(fires) > 1:
                    _log.warning("scheduler.coalesced", job=spec.name, skipped=len(fires) - 1)
                out.append(DueRun(spec, fires[-1], catch_up=False))
        return sorted(out, key=lambda d: (d.fire.scheduled_for, d.spec.name))

    def next_runs(self, now: datetime | None = None) -> list[NextRun]:
        now = now or self.clock.now()
        return [
            NextRun(s.name, s.action, s.trigger.describe(), s.trigger.next_fire(now))
            for s in self.specs
        ]

    def seconds_until_next_wake(self, now: datetime) -> float:
        wake = self.config.poll_seconds
        for spec in self.specs:
            nxt = spec.trigger.next_fire(now)
            if nxt is not None:
                wake = min(wake, (nxt.scheduled_for - now).total_seconds())
        return max(wake, 0.0)

    # ---- running -----------------------------------------------------------

    def run_pending(self) -> list[RunResult]:
        """Run everything due now, in fire order; stops early on shutdown.

        A job's cursor only moves past ``now`` once its due run has been
        attempted, so a store error part-way through a batch leaves the
        remaining fires due for the next wake-up instead of dropping them
        (the claim keeps the ones that did run from running twice).
        """
        now = self.clock.now()
        batch = self.due(now)
        waiting = {d.spec.name for d in batch}
        results = []
        self.pending = len(batch)
        try:
            for due in batch:
                if self.stopping:
                    _log.info("scheduler.stop_before_run", job=due.spec.name, run_key=due.fire.key)
                    break
                results.append(self.run_one(due.spec, due.fire, catch_up=due.catch_up))
                self.pending -= 1
                if due.catch_up:
                    self._startup.remove(due)
                if not any(d.spec.name == due.spec.name for d in batch[batch.index(due) + 1 :]):
                    waiting.discard(due.spec.name)
        finally:
            self.pending = 0
            for name in self._cursors:
                if name not in waiting:
                    self._cursors[name] = now
        return results

    def run_one(self, spec: JobSpec, fire: Fire, *, catch_up: bool = False) -> RunResult:
        started = self.clock.now()
        run_id = self.store.claim(
            spec, fire, instance_id=self.instance_id, now=started, catch_up=catch_up
        )
        if run_id is None:
            _log.info("scheduler.already_claimed", job=spec.name, run_key=fire.key)
            return RunResult(spec.name, None, fire, "already_claimed", catch_up, {})
        log = _log.bind(run_id=run_id, job=spec.name, run_key=fire.key, catch_up=catch_up)
        log.info("scheduler.run_started", scheduled_for=fire.scheduled_for.isoformat())
        if spec.ping_url:
            self.pinger.ping(spec.ping_url, "start", run_id=run_id)
        outcome = self._execute(spec, fire, run_id, started, log)
        error = outcome.detail.get("error") if outcome.status == "failed" else None
        self.store.finish(
            run_id, outcome.status, now=self.clock.now(), detail=outcome.detail, error=error
        )
        log.info("scheduler.run_finished", status=outcome.status)
        if spec.ping_url:
            event = "fail" if outcome.status == "failed" else "success"
            self.pinger.ping(spec.ping_url, event, run_id=run_id, body=str(outcome.detail))
        if outcome.status == "failed" and not outcome.alerted:
            self.notifier.notify(
                Notification(
                    level="error",
                    title=f"scheduled job {spec.name} failed",
                    message=str(error or outcome.detail),
                    fields={
                        "job": spec.name,
                        "run_id": run_id,
                        "run_key": fire.key,
                        "scheduled_for": fire.scheduled_for.isoformat(),
                    },
                )
            )
        return RunResult(spec.name, run_id, fire, outcome.status, catch_up, outcome.detail)

    def _execute(
        self, spec: JobSpec, fire: Fire, run_id: str, now: datetime, log: Any
    ) -> JobOutcome:
        ctx = RunContext(
            spec=spec,
            fire=fire,
            run_id=run_id,
            now=now,
            settings=self.settings,
            notifier=self.notifier,
            executor=self.executor,
        )
        try:
            return self.executor.execute(ctx)
        except Exception as exc:
            log.error("scheduler.run_raised", error=str(exc), error_type=type(exc).__name__)
            return JobOutcome("failed", {"error": f"{type(exc).__name__}: {exc}"})

    # ---- the loop ----------------------------------------------------------

    def run_forever(
        self,
        *,
        wait: Callable[[float], object] | None = None,
        watchdog: DeadlineWatchdog | None = None,
    ) -> None:
        """Loop until :meth:`request_stop`. ``wait(seconds)`` sleeps (the
        default waits on the stop event, so a stop wakes it at once)."""
        wait = wait or self._stop.wait
        if not self._started:
            self.start()
        dog_thread = self._start_background(watchdog)
        try:
            while not self.stopping:
                try:
                    self.run_pending()
                    self.store.heartbeat(self.instance_id, now=self.clock.now())
                except Exception as exc:  # e.g. a locked DB: retry next wake-up
                    _log.error(
                        "scheduler.iteration_failed", error=str(exc), error_type=type(exc).__name__
                    )
                now = self.clock.now()
                if self.stopping:
                    break
                wait(self.seconds_until_next_wake(now))
        finally:
            self._stop.set()
            dog_thread.join(timeout=5)
            self.stop()

    def _start_background(self, watchdog: DeadlineWatchdog | None) -> threading.Thread:
        """Heartbeat (so liveness holds while a long job runs) and the
        deadline watchdog, off the job thread."""

        def loop() -> None:
            while not self._stop.wait(self.config.watchdog_seconds):
                try:
                    now = self.clock.now()
                    self.store.heartbeat(self.instance_id, now=now)
                    if watchdog is not None:
                        watchdog.check(now)
                except Exception as exc:  # must outlive a bad check
                    _log.error("deadman.watchdog_failed", error=str(exc))

        thread = threading.Thread(target=loop, name="stonks-scheduler-watchdog", daemon=True)
        thread.start()
        return thread


def default_lock_path(config: SchedulerConfig, state_path: str | Path) -> Path:
    return (
        Path(config.lock_path) if config.lock_path else Path(state_path).parent / "scheduler.lock"
    )
