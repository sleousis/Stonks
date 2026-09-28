"""``python -m stonks.lab.offload``: the lab worker and its queue.

Commands::

    worker [--once] [--id ID]   pull lab jobs from the queue until SIGINT / SIGTERM
    worker --api URL [--work-dir DIR] [--once] [--id ID]
                                a worker on another machine: the queue, the
                                lake snapshot and the results go through the
                                API, with the token in STONKS_LAB_WORKER_TOKEN
    status                      queue and worker counts as JSON (exit 1 when unhealthy)
    snapshot                    publish a lake snapshot now (only while no
                                other process holds the lake, e.g. before `serve`)

``--config PATH`` picks the TOML file (default ``config/default.toml``).
The API queues jobs for workers when ``[lab.offload] executor = "worker"``
(env ``STONKS_LAB_EXECUTOR=worker``). See docs/deploy.md.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

EXIT_OK, EXIT_UNHEALTHY = 0, 1


def _settings(config_path: Path | None) -> Any:
    from dotenv import load_dotenv

    from stonks.config import load_settings
    from stonks.logging import configure_logging

    load_dotenv(override=False)
    settings = load_settings(config_path)
    configure_logging(level=settings.logging.level)
    return settings


def _cmd_worker(settings: Any, args: argparse.Namespace) -> int:
    tmp = os.environ.get("TMPDIR")
    if tmp:  # Compose points it at the data volume; create it before first use
        Path(tmp).mkdir(parents=True, exist_ok=True)
    if args.api:
        return _run_remote(settings, args)
    from stonks.lab.offload.worker import build_worker

    worker = build_worker(settings, worker_id=args.id)
    if args.once:
        worker.run_once()
        worker.queue.stop_worker(worker.worker_id)
        return EXIT_OK
    stop = threading.Event()

    def _stop(signum: int, frame: Any) -> None:
        stop.set()
        worker.request_stop()  # a running lab job stops and goes back to the queue

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    worker.run_forever(stop)
    return EXIT_OK


def _run_remote(settings: Any, args: argparse.Namespace) -> int:
    from stonks.lab.offload.api_client import TOKEN_ENV
    from stonks.lab.offload.remote_worker import build_remote_worker
    from stonks.scheduling.backends import API_TRUSTED_HOSTS_ENV

    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        print(f"set {TOKEN_ENV} to a lab_worker API token (docs/deploy.md)", file=sys.stderr)
        return EXIT_UNHEALTHY
    hosts = tuple(
        h.strip() for h in os.environ.get(API_TRUSTED_HOSTS_ENV, "").split(",") if h.strip()
    )
    try:
        worker = build_remote_worker(
            settings,
            api_url=args.api,
            token=token,
            worker_id=args.id,
            work_dir=args.work_dir,
            trusted_hosts=hosts,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_UNHEALTHY
    if args.once:
        worker.run_once()
        worker.stop()
        return EXIT_OK
    stop = threading.Event()

    def _stop(signum: int, frame: Any) -> None:
        stop.set()
        worker.request_stop()  # a running lab job stops and goes back to the queue

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    worker.run_forever(stop)
    return EXIT_OK


def _cmd_status(settings: Any, args: argparse.Namespace) -> int:
    from stonks.lab.offload.health import assess
    from stonks.lab.offload.queue import LabQueue
    from stonks.store.state import SqliteState

    with SqliteState(settings.state.path) as state:
        state.migrate()
    offload = settings.lab.offload
    stats = LabQueue(settings.state.path).stats(lease_seconds=offload.lease_seconds)
    ok, detail = assess(stats, stuck_minutes=settings.production.health.stuck_lab_queue_minutes)
    out = {"executor": offload.executor, "healthy": ok, "detail": detail, **asdict(stats)}
    print(json.dumps(out, indent=2))
    return EXIT_OK if ok else EXIT_UNHEALTHY


def _cmd_snapshot(settings: Any, args: argparse.Namespace) -> int:
    from stonks.lab.offload.snapshot import LakeSnapshots
    from stonks.store.lake import DuckDBLake

    offload = settings.lab.offload
    snaps = LakeSnapshots(offload.snapshot_root(settings.lake.path), keep=offload.keep_snapshots)
    with DuckDBLake(settings.lake.path) as lake:
        info = snaps.publish(lake)
    print(json.dumps({"snapshot": str(info.path), "created_at": info.created_at.isoformat()}))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m stonks.lab.offload", description=__doc__)
    parser.add_argument("--config", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    worker = sub.add_parser("worker", help="run a lab worker")
    worker.add_argument("--once", action="store_true", help="run at most one job, then exit")
    worker.add_argument("--id", default=None, help="worker id (default: host plus random)")
    worker.add_argument(
        "--api",
        default=None,
        metavar="URL",
        help="run on another machine against this Stonks API (token in STONKS_LAB_WORKER_TOKEN)",
    )
    worker.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="with --api: snapshots and scratch stores (default: <data dir>/lab_worker)",
    )
    sub.add_parser("status", help="queue and worker counts")
    sub.add_parser("snapshot", help="publish a lake snapshot now")
    return parser


_COMMANDS = {"worker": _cmd_worker, "status": _cmd_status, "snapshot": _cmd_snapshot}


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = _settings(args.config)
    return _COMMANDS[args.command](settings, args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
