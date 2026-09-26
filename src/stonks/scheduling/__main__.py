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
Stable until the ``stonks schedule ...`` CLI command wraps it.
"""

from __future__ import annotations

import argparse
import signal
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from stonks.scheduling.config import SchedulerConfig, scheduler_config_from
from stonks.scheduling.jobs import JobSpec, build_job_specs
from stonks.scheduling.runs import RunStore
from stonks.scheduling.triggers import Fire

EXIT_OK, EXIT_UNHEALTHY, EXIT_ALREADY_RUNNING, EXIT_USAGE = 0, 1, 2, 64


def _load(config_path: Path | None) -> tuple[Any, SchedulerConfig, list[JobSpec]]:
    from dotenv import load_dotenv

    from stonks.config import load_settings
    from stonks.logging import configure_logging

    load_dotenv(override=False)
    settings = load_settings(config_path)
    configure_logging(level=settings.logging.level)
    config = scheduler_config_from(settings, config_path)
    return settings, config, build_job_specs(config)


def _store(settings: Any) -> RunStore:
    store = RunStore(settings.state.path)
    store.migrate()
    return store


def _cmd_run(settings: Any, config: SchedulerConfig, specs: list[JobSpec]) -> int:
    from stonks.notify import notifier_from_settings
    from stonks.scheduling.deadman import DeadlineWatchdog, HttpPinger
    from stonks.scheduling.scheduler import (
        InstanceLock,
        Scheduler,
        SchedulerAlreadyRunningError,
        default_lock_path,
    )

    if not config.enabled:
        print("scheduler disabled ([scheduler].enabled = false)", file=sys.stderr)
        return EXIT_USAGE
    lock = InstanceLock(default_lock_path(config, settings.state.path))
    try:
        lock.acquire()
    except SchedulerAlreadyRunningError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ALREADY_RUNNING
    try:
        store = _store(settings)
        notifier = notifier_from_settings(settings)
        scheduler = Scheduler(
            specs,
            store,
            settings=settings,
            notifier=notifier,
            config=config,
            pinger=HttpPinger(timeout_seconds=config.ping_timeout_seconds),
        )
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: scheduler.request_stop())
        scheduler.run_forever(watchdog=DeadlineWatchdog(specs, store, notifier))
    finally:
        lock.release()
    return EXIT_OK


def _cmd_next(specs: list[JobSpec], now: datetime) -> int:
    for spec in specs:
        fire = spec.trigger.next_fire(now)
        when = fire.scheduled_for.isoformat() if fire else "-"
        as_of = fire.as_of.isoformat() if fire else "-"
        print(f"{spec.name:<20} {when:<27} as_of={as_of:<10} {spec.trigger.describe()}")
    return EXIT_OK


def _cmd_runs(settings: Any, job: str | None, limit: int) -> int:
    for r in _store(settings).recent(job_name=job, limit=limit):
        print(
            f"{r.job_name:<20} {r.run_key:<27} {r.status:<9} "
            f"{'catch-up ' if r.catch_up else ''}{r.error or ''}".rstrip()
        )
    return EXIT_OK


def _cmd_run_now(
    settings: Any, config: SchedulerConfig, specs: list[JobSpec], job: str, as_of: date | None
) -> int:
    from stonks.notify import notifier_from_settings
    from stonks.scheduling.deadman import HttpPinger
    from stonks.scheduling.scheduler import Scheduler

    spec = next((s for s in specs if s.name == job), None)
    if spec is None:
        print(f"unknown job {job!r}; jobs: {[s.name for s in specs]}", file=sys.stderr)
        return EXIT_USAGE
    now = datetime.now(UTC)
    fire = Fire(now, as_of or now.date(), f"manual:{now.isoformat()}")
    scheduler = Scheduler(
        specs,
        _store(settings),
        settings=settings,
        notifier=notifier_from_settings(settings),
        config=config,
        pinger=HttpPinger(timeout_seconds=config.ping_timeout_seconds),
    )
    result = scheduler.run_one(spec, fire)
    print(f"{spec.name} {result.status} {result.detail}")
    return EXIT_OK if result.status in ("succeeded", "skipped") else EXIT_UNHEALTHY


def _cmd_check(settings: Any, specs: list[JobSpec], now: datetime) -> int:
    from stonks.notify import notifier_from_settings
    from stonks.scheduling.deadman import DeadlineWatchdog, missed_deadlines

    store = _store(settings)
    DeadlineWatchdog(specs, store, notifier_from_settings(settings)).check(now)
    misses = missed_deadlines(specs, store, now, not_before=store.first_started_at())
    for m in misses:
        print(f"MISSED {m.job_name} {m.fire.key}: {m.reason}")
    return EXIT_UNHEALTHY if misses else EXIT_OK


def _cmd_metrics(settings: Any, specs: list[JobSpec], data_age: bool) -> int:
    from stonks.scheduling.metrics import latest_daily_bars, metrics_text

    bars = None
    if data_age:
        from stonks.store.lake import DuckDBLake

        with DuckDBLake(settings.lake.path) as lake:
            bars = latest_daily_bars(lake, list(settings.production.universe))
    sys.stdout.write(metrics_text(settings.state.path, specs=specs, latest_bars=bars))
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
    metrics.add_argument("--data-age", action="store_true", help="also read bar ages from the lake")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings, config, specs = _load(args.config)
    now = datetime.now(UTC)
    if args.command == "run":
        return _cmd_run(settings, config, specs)
    if args.command == "next":
        return _cmd_next(specs, now)
    if args.command == "runs":
        return _cmd_runs(settings, args.job, args.limit)
    if args.command == "run-now":
        return _cmd_run_now(settings, config, specs, args.job, args.as_of)
    if args.command == "check":
        return _cmd_check(settings, specs, now)
    return _cmd_metrics(settings, specs, args.data_age)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
