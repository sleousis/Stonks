"""``python -m stonks.engine``: run, replay, check or stop the intraday
engine (roadmap 21.2.5).

- ``run [--session D]``: the live engine for a session. It takes the engine
  lock, streams through ``[streaming]``, trades the ``[engine]`` books and
  stops itself at the close plus ``stop_after_close_minutes``. The
  scheduler's ``engine_start`` job starts this.
- ``replay PATH [--session D]``: a recording through the same loop, on a
  fake clock, with simulated books only.
- ``status``: whether an engine runs, and its latest run row.
- ``stop``: ask the running engine to stop and wait for it.

Exit codes: 0 done, 2 not configured or off, 3 another engine runs,
4 the start failed (an order still unknown after reconciliation).
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from stonks.config import Settings
    from stonks.core.protocols import Strategy
    from stonks.engine.process import EngineProcess


def _settings() -> Settings:
    from dotenv import load_dotenv

    from stonks.config import load_settings
    from stonks.config_overrides import with_overrides

    load_dotenv(override=False)
    # the admin's console overrides (risk limits among them) on top of TOML,
    # as the CLI and the scheduler apply them
    return with_overrides(load_settings())


def _session(raw: str | None) -> date:
    return date.fromisoformat(raw) if raw else datetime.now(UTC).date()


def _control(settings: Settings) -> Any:
    from stonks.engine.control import EngineControl
    from stonks.engine.settings import control_dir_for

    return EngineControl(control_dir_for(settings.engine, settings.state.path))


def _on_signals(process: EngineProcess) -> None:
    def handler(signum: int, _frame: Any) -> None:
        process.stop(f"signal {signum}")

    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except ValueError:  # not the main thread (tests)
                return


def _serve(
    settings: Settings,
    session: date,
    *,
    replay: str | None = None,
    speed: float | None = None,
    write_bars: bool = False,
    strategies: Mapping[str, Strategy] | None = None,
) -> int:
    from stonks.core.clock import SYSTEM_CLOCK, FakeClock
    from stonks.engine.control import EngineAlreadyRunningError
    from stonks.engine.process import build_engine, session_window
    from stonks.execution.order_state import ReconciliationPendingError
    from stonks.store.lake import DuckDBLake
    from stonks.store.state import SqliteState
    from stonks.streaming.runner import build_runner
    from stonks.streaming.sources.replay import ReplaySource

    cfg = settings.engine
    if not cfg.books:
        print("no engine books: add [[engine.books]] first", file=sys.stderr)
        return 2
    control = _control(settings)
    try:
        lock = control.acquire()
    except EngineAlreadyRunningError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    try:
        window = session_window(cfg.calendar, session)
        if replay is None and window is None:
            print(f"{session} is not a session on {cfg.calendar}: nothing to trade")
            return 0
        with DuckDBLake(settings.lake.path) as lake, SqliteState(settings.state.path) as state:
            if replay is not None:
                start = window[0] if window else datetime(*session.timetuple()[:3], tzinfo=UTC)
                clock: Any = FakeClock(start)
                source = ReplaySource(replay, speed=speed)
                process = build_engine(
                    settings,
                    lake=lake,
                    state=state,
                    session=session,
                    source=source,
                    clock=clock,
                    control=control,
                    strategies=strategies,
                    write_bars=write_bars,
                    brokers={"ibkr": _no_replay_broker},
                )
            else:
                runner = build_runner(
                    settings, lake=lake, state=state, clock=SYSTEM_CLOCK, source_id=cfg.source
                )
                runner.tickers = list(cfg.universe or settings.streaming.tickers)
                process = build_engine(
                    settings,
                    lake=lake,
                    state=state,
                    session=session,
                    runner=runner,
                    control=control,
                    strategies=strategies,
                )
            _on_signals(process)
            try:
                process.start()
            except ReconciliationPendingError as exc:
                print(f"the engine did not start: {exc}", file=sys.stderr)
                return 4
            stats = process.run()
            print(json.dumps({"run_id": process.run_id, **stats.snapshot()}, indent=2))
    finally:
        lock.release()
    return 0


def _no_replay_broker(settings: Settings, portfolio_id: str) -> Any:
    raise ValueError(f"a replay trades simulated books only ({portfolio_id} is at a broker)")


def _status(settings: Settings) -> int:
    from stonks.engine.recovery import EngineRuns
    from stonks.store.state import SqliteState

    control = _control(settings)
    with SqliteState(settings.state.path) as state:
        latest = EngineRuns(state).latest()
    out: dict[str, Any] = {"running": control.running(), "enabled": settings.engine.enabled}
    if latest is not None:
        out["latest"] = {
            "id": latest.id,
            "session": latest.session_date.isoformat(),
            "mode": latest.mode,
            "status": latest.status,
            "last_close_at": latest.last_close_at.isoformat() if latest.last_close_at else None,
            "bar_closes": latest.bar_closes,
            "orders_routed": latest.orders_routed,
            "heartbeat_at": latest.heartbeat_at,
        }
    print(json.dumps(out, indent=2))
    return 0


def _stop(settings: Settings, timeout: float | None) -> int:
    control = _control(settings)
    if not control.running():
        print("no engine is running")
        return 0
    control.request_stop("python -m stonks.engine stop")
    wait = settings.engine.stop_timeout_seconds if timeout is None else timeout
    if control.wait_stopped(wait):
        print("the engine stopped")
        return 0
    print(f"the engine is still running after {wait:g} s", file=sys.stderr)
    return 1


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    strategies: Mapping[str, Strategy] | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="python -m stonks.engine")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="the live engine for a session")
    run.add_argument("--session", help="YYYY-MM-DD, default today (UTC)")
    rep = sub.add_parser("replay", help="a recording through the engine on a fake clock")
    rep.add_argument("path")
    rep.add_argument("--session", help="YYYY-MM-DD, default today (UTC)")
    rep.add_argument("--speed", type=float, help="1.0 waits the recorded gaps")
    rep.add_argument("--write-bars", action="store_true", help="store the bars in the lake")
    sub.add_parser("status", help="whether an engine runs, and its latest run")
    stop = sub.add_parser("stop", help="stop the running engine")
    stop.add_argument("--timeout", type=float)
    args = parser.parse_args(argv)

    settings = settings or _settings()
    if args.command == "status":
        return _status(settings)
    if args.command == "stop":
        return _stop(settings, args.timeout)
    if args.command == "run":
        if not settings.engine.enabled:
            print("the engine is off: set [engine] enabled = true first", file=sys.stderr)
            return 2
        return _serve(settings, _session(args.session), strategies=strategies)
    return _serve(
        settings,
        _session(args.session),
        replay=args.path,
        speed=args.speed,
        write_bars=args.write_bars,
        strategies=strategies,
    )


if __name__ == "__main__":
    raise SystemExit(main())
