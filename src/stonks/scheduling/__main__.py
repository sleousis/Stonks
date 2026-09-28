"""``python -m stonks.scheduling``: run and inspect the built-in scheduler.

Commands::

    run                   run the scheduler until SIGINT / SIGTERM
    next                  list jobs with their triggers and next fire times
    runs [--job J]        recent scheduled runs
    run-now JOB [--as-of YYYY-MM-DD]
                          run one job immediately (recorded as a manual run)
    check                 deadline watchdog once (alerts, exit 1 on a miss)
    metrics [--data-age]  Prometheus text metrics on stdout

``--config PATH`` picks the TOML file (default ``config/default.toml``).
Jobs run on the backend ``[scheduler].backend`` resolves to: ``api`` when
``STONKS_API_URL`` (or ``[scheduler].api_url``) is set, else ``local``.
``stonks schedule ...`` is the same command in the main CLI.
"""

from __future__ import annotations

import argparse
import signal
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from stonks.scheduling.config import SchedulerConfig, scheduler_config_from
from stonks.scheduling.jobs import JobExecutor, JobSpec, build_job_specs
from stonks.scheduling.runs import MANUAL_PREFIX, RunStore
from stonks.scheduling.triggers import Fire

EXIT_OK, EXIT_UNHEALTHY, EXIT_ALREADY_RUNNING, EXIT_USAGE = 0, 1, 2, 64


@dataclass
class _Loaded:
    settings: Any
    config: SchedulerConfig
    executor: JobExecutor
    specs: list[JobSpec]


def _load(config_path: Path | None, transport: Any = None) -> _Loaded:
    from dotenv import load_dotenv

    from stonks.config import load_settings
    from stonks.logging import configure_logging
    from stonks.scheduling.backends import build_executor

    load_dotenv(override=False)
    settings = load_settings(config_path)
    configure_logging(level=settings.logging.level)
    config = scheduler_config_from(settings, config_path)
    executor = build_executor(config, transport=transport)
    specs = build_job_specs(config, actions=executor.actions())
    return _Loaded(settings, config, executor, specs)


def _store(settings: Any) -> RunStore:
    store = RunStore(settings.state.path)
    store.migrate()
    return store


def _watchdog(ld: _Loaded, store: RunStore, notifier: Any) -> Any:
    """The deadline watchdog plus the engine dead-man (roadmap 21.3.4)."""
    from stonks.engine.deadman import engine_deadman_from_settings
    from stonks.scheduling.deadman import DeadlineWatchdog

    return DeadlineWatchdog(
        ld.specs,
        store,
        notifier,
        extra_checks=[engine_deadman_from_settings(ld.settings, store, notifier)],
    )


def _scheduler(ld: _Loaded, store: RunStore, notifier: Any) -> Any:
    from stonks.scheduling.deadman import HttpPinger
    from stonks.scheduling.scheduler import Scheduler

    return Scheduler(
        ld.specs,
        store,
        settings=ld.settings,
        notifier=notifier,
        config=ld.config,
        pinger=HttpPinger(timeout_seconds=ld.config.ping_timeout_seconds),
        executor=ld.executor,
    )


def _cmd_run(ld: _Loaded) -> int:
    from stonks.notify import notifier_from_settings
    from stonks.scheduling.delivery import start_delivery_worker
    from stonks.scheduling.scheduler import (
        InstanceLock,
        SchedulerAlreadyRunningError,
        default_lock_path,
    )

    if not ld.config.enabled:
        print("scheduler disabled ([scheduler].enabled = false)", file=sys.stderr)
        return EXIT_USAGE
    lock = InstanceLock(default_lock_path(ld.config, ld.settings.state.path))
    try:
        lock.acquire()
    except SchedulerAlreadyRunningError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ALREADY_RUNNING
    try:
        store = _store(ld.settings)
        notifier = notifier_from_settings(ld.settings)
        scheduler = _scheduler(ld, store, notifier)
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: scheduler.request_stop())
        delivery = start_delivery_worker(ld.settings)
        try:
            scheduler.run_forever(watchdog=_watchdog(ld, store, notifier))
        finally:
            if delivery is not None:
                delivery.stop()
    finally:
        lock.release()
    return EXIT_OK


def _cmd_next(ld: _Loaded, now: datetime) -> int:
    print(f"backend: {ld.executor.backend}")
    for spec in ld.specs:
        fire = spec.trigger.next_fire(now)
        when = fire.scheduled_for.isoformat() if fire else "-"
        as_of = fire.as_of.isoformat() if fire else "-"
        print(f"{spec.name:<20} {when:<27} as_of={as_of:<10} {spec.trigger.describe()}")
    return EXIT_OK


def _cmd_runs(ld: _Loaded, job: str | None, limit: int) -> int:
    for r in _store(ld.settings).recent(job_name=job, limit=limit):
        print(
            f"{r.job_name:<20} {r.run_key:<27} {r.status:<9} "
            f"{'catch-up ' if r.catch_up else ''}{r.error or ''}".rstrip()
        )
    return EXIT_OK


def _cmd_run_now(ld: _Loaded, job: str, as_of: date | None) -> int:
    from stonks.notify import notifier_from_settings

    spec = next((s for s in ld.specs if s.name == job), None)
    if spec is None:
        print(f"unknown job {job!r}; jobs: {[s.name for s in ld.specs]}", file=sys.stderr)
        return EXIT_USAGE
    now = datetime.now(UTC)
    fire = Fire(now, as_of or now.date(), f"{MANUAL_PREFIX}{now.isoformat()}")
    scheduler = _scheduler(ld, _store(ld.settings), notifier_from_settings(ld.settings))
    try:
        result = scheduler.run_one(spec, fire)
    finally:
        ld.executor.close()
    print(f"{spec.name} {result.status} {result.detail}")
    return EXIT_OK if result.status in ("succeeded", "skipped") else EXIT_UNHEALTHY


def _cmd_check(ld: _Loaded, now: datetime) -> int:
    from stonks.notify import notifier_from_settings
    from stonks.scheduling.deadman import missed_deadlines

    store = _store(ld.settings)
    _watchdog(ld, store, notifier_from_settings(ld.settings)).check(now)
    misses = missed_deadlines(ld.specs, store, now, not_before=store.first_started_at())
    for m in misses:
        print(f"MISSED {m.job_name} {m.fire.key}: {m.reason}")
    return EXIT_UNHEALTHY if misses else EXIT_OK


def _cmd_metrics(ld: _Loaded, data_age: bool) -> int:
    from stonks.scheduling.metrics import latest_daily_bars, metrics_text

    bars = None
    if data_age:
        # Opens the lake: only while no `stonks serve` holds it.
        from stonks.production.universe import EmptyUniverseError, production_tickers
        from stonks.store.lake import DuckDBLake

        with DuckDBLake(ld.settings.lake.path) as lake:
            try:
                universe = production_tickers(
                    lake, ld.settings.production.universe, datetime.now(UTC).date()
                )
            except EmptyUniverseError:
                universe = []
            bars = latest_daily_bars(lake, universe)
    sys.stdout.write(metrics_text(ld.settings.state.path, specs=ld.specs, latest_bars=bars))
    return EXIT_OK


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m stonks.scheduling")
    p.add_argument("--config", type=Path, default=None, help="TOML config file")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run the scheduler until stopped")
    sub.add_parser("next", help="jobs and their next fire times")
    runs = sub.add_parser("runs", help="recent scheduled runs")
    runs.add_argument("--job", default=None)
    runs.add_argument("--limit", type=int, default=20)
    now = sub.add_parser("run-now", help="run one job immediately")
    now.add_argument("job")
    now.add_argument("--as-of", type=date.fromisoformat, default=None)
    sub.add_parser("check", help="deadline watchdog once")
    metrics = sub.add_parser("metrics", help="Prometheus metrics")
    metrics.add_argument(
        "--data-age", action="store_true", help="also read bar ages from the lake (local only)"
    )
    return p


def main(
    argv: Sequence[str] | None = None, *, transport: Any = None, prog: str | None = None
) -> int:
    """``transport`` is an ``httpx2`` transport for the ``api`` backend (tests);
    ``prog`` names the command in usage messages."""
    from stonks.scheduling.backends import BackendConfigError
    from stonks.scheduling.jobs import UnknownActionError

    parser = _parser()
    if prog is not None:
        parser.prog = prog
    args = parser.parse_args(argv)
    try:
        ld = _load(args.config, transport)
    except (BackendConfigError, UnknownActionError, ValueError) as exc:
        print(f"stonks.scheduling: {exc}", file=sys.stderr)
        return EXIT_USAGE
    now = datetime.now(UTC)
    if args.command == "run":
        return _cmd_run(ld)
    if args.command == "next":
        return _cmd_next(ld, now)
    if args.command == "runs":
        return _cmd_runs(ld, args.job, args.limit)
    if args.command == "run-now":
        return _cmd_run_now(ld, args.job, args.as_of)
    if args.command == "check":
        return _cmd_check(ld, now)
    return _cmd_metrics(ld, args.data_age)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
