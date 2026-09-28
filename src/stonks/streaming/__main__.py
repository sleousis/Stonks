"""``python -m stonks.streaming``: operator entry points for live streams
(roadmap 21.1).

python -m stonks.streaming sources
    List the registered streaming sources.

python -m stonks.streaming run [--source ID] [--tickers A,B] [--record] [--seconds N]
    Stream into 1m bars in the lake until Ctrl-C (or N seconds), with
    reconnects and gap backfills. Needs ``[streaming] enabled = true``. The
    lake file must not be held by another writer (``stonks serve``), unless
    the bars live in the Parquet store.

python -m stonks.streaming record --tickers A,B [--dir D] [--source ID] [--seconds N]
    Save a stream to Parquet only. Writes nothing to the lake.

python -m stonks.streaming replay DIR [--tickers A,B] [--write]
    Play a recording back through the bar builder. ``--write`` stores the
    bars in the lake. Prints the health summary as JSON.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from stonks.config import Settings
    from stonks.streaming.runner import StreamRunner


def _settings() -> Settings:
    from dotenv import load_dotenv

    from stonks.config import load_settings

    load_dotenv(override=False)
    return load_settings()


def _tickers(raw: str | None, settings: Settings) -> list[str]:
    if raw:
        return [t.strip() for t in raw.split(",") if t.strip()]
    return list(settings.streaming.tickers)


def _run_for(runner: StreamRunner, seconds: float | None) -> None:
    timer = threading.Timer(seconds, runner.stop) if seconds else None
    if timer is not None:
        timer.daemon = True
        timer.start()
    try:
        runner.run()
    except KeyboardInterrupt:
        runner.stop()
    finally:
        if timer is not None:
            timer.cancel()
    print(json.dumps(runner.health.snapshot(), indent=2))


def _sources() -> int:
    from stonks.streaming.registry import stream_source_classes

    for source_id, cls in stream_source_classes().items():
        kind = "finite" if cls.finite else "live"
        print(f"{source_id}\t{kind}\t{cls.__module__}")
    return 0


def _run(args: argparse.Namespace) -> int:
    from stonks.store.lake import DuckDBLake
    from stonks.store.state import SqliteState
    from stonks.streaming.runner import build_runner

    settings = _settings()
    if not settings.streaming.enabled:
        print("streaming is off: set [streaming] enabled = true first", file=sys.stderr)
        return 2
    settings.streaming.tickers = _tickers(args.tickers, settings)
    if not settings.streaming.tickers:
        print("no tickers: pass --tickers or set [streaming] tickers", file=sys.stderr)
        return 2
    with DuckDBLake(settings.lake.path) as lake, SqliteState(settings.state.path) as state:
        runner = build_runner(
            settings, lake=lake, state=state, source_id=args.source, record=args.record or None
        )
        _run_for(runner, args.seconds)
    return 0


def _record(args: argparse.Namespace) -> int:
    from stonks.streaming.runner import build_runner

    settings = _settings()
    settings.streaming.tickers = _tickers(args.tickers, settings)
    if not settings.streaming.tickers:
        print("no tickers: pass --tickers or set [streaming] tickers", file=sys.stderr)
        return 2
    if args.dir:
        settings.streaming.record.dir = args.dir
    settings.streaming.backfill = False
    runner = build_runner(settings, source_id=args.source, record=True)
    _run_for(runner, args.seconds)
    return 0


def _replay(args: argparse.Namespace) -> int:
    from stonks.core.clock import FakeClock
    from stonks.store.lake import DuckDBLake
    from stonks.streaming.runner import StreamRunner
    from stonks.streaming.sources.replay import ReplaySource

    settings = _settings()
    clock = FakeClock(datetime(1970, 1, 1, tzinfo=UTC))
    source = ReplaySource(args.dir, clock=clock)
    cfg = settings.streaming.model_copy(update={"backfill": False})
    tickers = _tickers(args.tickers, settings) if args.tickers else []
    if args.write:
        with DuckDBLake(settings.lake.path) as lake:
            runner = StreamRunner(source, cfg, tickers=tickers, store=lake.bar_store, clock=clock)
            runner.run()
    else:
        runner = StreamRunner(source, cfg, tickers=tickers, clock=clock)
        runner.run()
    print(json.dumps(runner.health.snapshot(), indent=2))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m stonks.streaming")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sources", help="list the registered streaming sources")

    run = sub.add_parser("run", help="stream into 1m bars in the lake")
    run.add_argument("--source")
    run.add_argument("--tickers")
    run.add_argument("--record", action="store_true", help="also save the stream to Parquet")
    run.add_argument("--seconds", type=float)

    rec = sub.add_parser("record", help="save a stream to Parquet only")
    rec.add_argument("--source")
    rec.add_argument("--tickers")
    rec.add_argument("--dir")
    rec.add_argument("--seconds", type=float)

    rep = sub.add_parser("replay", help="play a recording through the bar builder")
    rep.add_argument("dir")
    rep.add_argument("--tickers")
    rep.add_argument("--write", action="store_true", help="store the bars in the lake")

    args = parser.parse_args(argv)
    if args.command == "sources":
        return _sources()
    if args.command == "run":
        return _run(args)
    if args.command == "record":
        return _record(args)
    return _replay(args)


if __name__ == "__main__":
    raise SystemExit(main())
