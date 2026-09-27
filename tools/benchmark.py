"""Reproduce the capacity measurements in docs/capacity.md (roadmap 14.10).

Everything runs on seeded, synthetic data in a temp folder. No network.

    uv run python -m tools.benchmark all --out benchmark.json
    uv run python -m tools.benchmark storage [--tickers 50 --years 5]
    uv run python -m tools.benchmark tick [--strategies 1,5,20 --portfolios 1,5,20]
    uv run python -m tools.benchmark lab [--presets quick,standard,promotion --workers 1]
    uv run python -m tools.benchmark api [--seconds 15]

Sections:

- ``storage``: lake bytes per ticker-year of daily bars, for the DuckDB bar
  table and the Parquet bar store.
- ``tick``: seconds per production tick as strategies and portfolios grow,
  and state DB bytes each tick adds.
- ``lab``: seconds per lab run for each survival preset.
- ``api``: starts the seeded e2e server, measures its memory, and API
  latency under light load (``tools.api_load``): idle, with a lab run inside
  the API, and with the same run on a lab worker (roadmap 14.9).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Iterator, Sequence
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

MB = 1024 * 1024
TRADING_DAYS = 252


# ---- seeding --------------------------------------------------------------------------


def tickers(n: int) -> list[str]:
    return [f"T{i:04d}.US" for i in range(n)]


def seed_lake(path: Path, names: Sequence[str], periods: int, *, end: date | None = None) -> None:
    """A migrated lake with daily random-walk bars and an equity row per ticker."""
    from stonks.store.lake import DuckDBLake

    rng = np.random.default_rng(7)
    days = pd.bdate_range(end=end or date(2026, 9, 25), periods=periods)
    lake = DuckDBLake(path)
    try:
        lake.migrate()
        frames = []
        for ticker in names:
            close = 50.0 * np.exp(np.cumsum(rng.normal(0.0004, 0.015, len(days))))
            frames.append(
                pd.DataFrame(
                    {
                        "ticker": ticker,
                        "date": [d.date() for d in days],
                        "open": close,
                        "high": close * 1.01,
                        "low": close * 0.99,
                        "close": close,
                        "adj_close": close,
                        "volume": 1_000_000,
                    }
                )
            )
        if frames:
            lake.upsert_prices(pd.concat(frames, ignore_index=True))
            lake.con.executemany(
                "INSERT INTO instruments (id, asset_class) VALUES (?, 'equity')",
                [[t] for t in names],
            )
        lake.con.execute("CHECKPOINT")
    finally:
        lake.close()


def dir_bytes(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    if not path.exists():
        return 0
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def lake_bytes(lake_path: Path) -> int:
    """The lake file plus its WAL (the Parquet bars are counted apart)."""
    wal = lake_path.with_name(lake_path.name + ".wal")
    return dir_bytes(lake_path) + dir_bytes(wal)


@contextlib.contextmanager
def workdir(keep: Path | None = None) -> Iterator[Path]:
    if keep is not None:
        keep.mkdir(parents=True, exist_ok=True)
        yield keep
        return
    path = Path(tempfile.mkdtemp(prefix="stonks-bench-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def settings_for(root: Path, universe: Sequence[str], *, lab_workers: int = 1) -> Any:
    from stonks.config import (
        ApiConfig,
        LakeConfig,
        ProductionConfig,
        RegistryConfig,
        Settings,
        StateConfig,
    )
    from stonks.lab.parallel import ParallelSettings

    settings = Settings(
        lake=LakeConfig(path=root / "lake.duckdb"),
        state=StateConfig(path=root / "state.sqlite"),
        registry=RegistryConfig(artifacts_dir=root / "artifacts"),
        api=ApiConfig(ui_dist=root / "no-dist"),
        production=ProductionConfig(universe=list(universe), initial_cash=100_000.0),
    )
    settings.lab = settings.lab.model_copy(
        update={"parallel": ParallelSettings(max_workers=lab_workers)}
    )
    return settings


# ---- storage --------------------------------------------------------------------------


def bench_storage(n_tickers: int = 50, years: int = 5, keep: Path | None = None) -> dict[str, Any]:
    """Lake bytes per ticker-year of daily bars, both bar stores."""
    from stonks.store.lake import DuckDBLake

    with workdir(keep) as root:
        empty = root / "empty" / "lake.duckdb"
        empty.parent.mkdir()
        seed_lake(empty, [], 1)
        base = lake_bytes(empty)
        path = root / "full" / "lake.duckdb"
        path.parent.mkdir()
        started = time.perf_counter()
        seed_lake(path, tickers(n_tickers), years * TRADING_DAYS)
        seed_seconds = time.perf_counter() - started
        duck = lake_bytes(path) - base
        with DuckDBLake(path) as lake:
            lake.migrate_bars_to_parquet()
            lake.con.execute("CHECKPOINT")
            parquet_files = dir_bytes(lake.bars_root)
        ticker_years = n_tickers * years
        return {
            "tickers": n_tickers,
            "years": years,
            "rows": n_tickers * years * TRADING_DAYS,
            "seed_seconds": round(seed_seconds, 2),
            "duckdb_bytes_per_ticker_year": round(duck / ticker_years),
            # One file per ticker and year: the bar files alone (the lake
            # file keeps its freed blocks after the move, so it is left out).
            "parquet_bytes_per_ticker_year": round(parquet_files / ticker_years),
            "empty_lake_bytes": base,
        }


# ---- tick -----------------------------------------------------------------------------


def _seed_book(settings: Any, universe: Sequence[str], n_strategies: int, n_portfolios: int):
    from stonks.accounts import DEFAULT_OWNER_ID, Mode, Role, Scope
    from stonks.accounts.default_book import ensure_default_subscription
    from stonks.accounts.portfolios import PortfolioRepository
    from stonks.accounts.subscriptions import SubscriptionRepository
    from stonks.registry.store import StrategyRegistry
    from stonks.store.state import SqliteState
    from stonks.strategies.examples.momentum import Momentum

    with SqliteState(settings.state.path) as state:
        state.migrate()
        registry = StrategyRegistry(state=state, artifacts_dir=settings.registry.artifacts_dir)
        ids = []
        for i in range(n_strategies):
            sid = registry.register(
                Momentum({"lookback_days": 40 + 5 * i, "skip_days": 5, "allocation": 0.5}),
                reports=[],
                strategy_id=f"mom_{i:02d}",
            )
            registry.set_status(
                sid,
                "active",
                actor="bench",
                reason="seeded for the capacity benchmark",
                override=True,
            )
            ensure_default_subscription(state, sid, Mode.PAPER)
            ids.append(sid)
        owner = Scope(user_id=DEFAULT_OWNER_ID, role=Role.ADMIN)
        subs = SubscriptionRepository(state)
        for p in range(n_portfolios - 1):
            pf = PortfolioRepository(state).create(owner, name=f"bench {p}", initial_cash=50_000.0)
            for sid in ids:
                subs.subscribe(owner, strategy_id=sid, mode=Mode.PAPER, portfolio_id=pf.id)


def bench_tick_once(
    n_strategies: int, n_portfolios: int, *, n_tickers: int = 30, ticks: int = 4
) -> dict[str, Any]:
    """Median seconds per real (paper) tick, and state bytes per tick."""
    from stonks.app.context import AppContext
    from stonks.app.services import Services
    from stonks.app.ticks import TickRequest

    with workdir() as root:
        universe = tickers(n_tickers)
        seed_lake(root / "lake.duckdb", universe, 320)
        settings = settings_for(root, universe)
        _seed_book(settings, universe, n_strategies, n_portfolios)
        services = Services.create(AppContext(settings))
        services.start()
        days = [d.date() for d in pd.bdate_range(end=date(2026, 9, 25), periods=ticks + 1)]
        durations = []
        try:
            services.ticks.run(TickRequest(as_of=days[0]))  # warm-up: imports, caches
            state_before = dir_bytes(settings.state.path) + dir_bytes(
                Path(str(settings.state.path) + "-wal")
            )
            for day in days[1:]:
                started = time.perf_counter()
                services.ticks.run(TickRequest(as_of=day))
                durations.append(time.perf_counter() - started)
        finally:
            services.shutdown()
        from stonks.store.state import SqliteState

        with SqliteState(settings.state.path) as state:
            state.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        state_after = dir_bytes(settings.state.path)
        median = statistics.median(durations)
        return {
            "strategies": n_strategies,
            "portfolios": n_portfolios,
            "tickers": n_tickers,
            "tick_seconds": round(median, 3),
            "seconds_per_portfolio": round(median / n_portfolios, 3),
            "seconds_per_strategy": round(median / n_strategies, 3),
            "state_bytes_per_tick": max(0, round((state_after - state_before) / len(durations))),
        }


def bench_tick(
    strategies: Sequence[int] = (1, 5, 20),
    portfolios: Sequence[int] = (1, 5, 20),
    *,
    n_tickers: int = 30,
    ticks: int = 4,
) -> list[dict[str, Any]]:
    """Strategies grow with one portfolio, then portfolios grow with 5 strategies."""
    runs = [bench_tick_once(s, 1, n_tickers=n_tickers, ticks=ticks) for s in strategies]
    mid = 5 if 5 in strategies else strategies[0]
    runs += [
        bench_tick_once(mid, p, n_tickers=n_tickers, ticks=ticks) for p in portfolios if p != 1
    ]
    return runs


# ---- lab ------------------------------------------------------------------------------


def bench_lab(
    presets: Sequence[str] = ("quick", "standard", "promotion"),
    *,
    n_tickers: int = 10,
    days: int = 500,
    budget: int = 10,
    workers: int = 1,
) -> list[dict[str, Any]]:
    """Seconds per momentum lab run for each survival preset."""
    from stonks.app.context import AppContext
    from stonks.app.lab import LabRunRequest
    from stonks.app.services import Services
    from stonks.app.strategies import StrategyRef

    with workdir() as root:
        universe = tickers(n_tickers)
        seed_lake(root / "lake.duckdb", universe, days)
        settings = settings_for(root, universe, lab_workers=workers)
        services = Services.create(AppContext(settings))
        services.start()
        span = pd.bdate_range(end=date(2026, 9, 25), periods=days)
        out = []
        try:
            for preset in presets:
                request = LabRunRequest(
                    strategy=StrategyRef(class_path="stonks.strategies.examples.momentum:Momentum"),
                    universe=universe,
                    start=span[0].date(),
                    end=span[-1].date(),
                    tuner="random",
                    budget=budget,
                    preset=preset,  # type: ignore[arg-type]
                    benchmark="EW",
                    preflight=False,
                )
                started = time.perf_counter()
                view = services.lab.run_lab(request)
                out.append(
                    {
                        "preset": preset,
                        "tickers": n_tickers,
                        "bars": days,
                        "budget": budget,
                        "workers": workers,
                        "tests": len(view.survival_reports),
                        "seconds": round(time.perf_counter() - started, 1),
                    }
                )
        finally:
            services.shutdown()
        return out


# ---- api ------------------------------------------------------------------------------


def rss_bytes(pid: int) -> int | None:
    """Resident memory of ``pid`` and its children (psutil when installed,
    else the OS, ``pid`` alone). Children count because a Windows venv
    ``python.exe`` is a small launcher that starts the real interpreter."""
    try:
        import psutil  # type: ignore[import-not-found]

        proc = psutil.Process(pid)
        tree = [proc, *proc.children(recursive=True)]
        return int(sum(p.memory_info().rss for p in tree))
    except ImportError:
        pass
    except Exception:
        return None
    status = Path(f"/proc/{pid}/status")
    if status.exists():
        for line in status.read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    if platform.system() == "Windows":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        fields = [f.strip('"') for f in out.strip().split('","')]
        if len(fields) >= 5:
            return int("".join(ch for ch in fields[4] if ch.isdigit())) * 1024
    return None


def _post(base: str, path: str, token: str, body: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _get(base: str, path: str, token: str) -> dict[str, Any]:
    req = urllib.request.Request(base + path, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _lab_body(stack: Any) -> dict[str, Any]:
    from tests.e2e.fake_market import EQUITIES

    span = pd.bdate_range(end=stack.market_end, periods=400)
    return {
        "strategy": {"class_path": "stonks.strategies.examples.momentum:Momentum"},
        "universe": list(EQUITIES),
        "start": span[0].date().isoformat(),
        "end": span[-1].date().isoformat(),
        "tuner": "random",
        "budget": 1000,
        "preset": "quick",
        "benchmark": "EW",
    }


def _wait_running(base: str, token: str, job_id: str, timeout: float = 60.0) -> str:
    deadline = time.monotonic() + timeout
    status = "queued"
    while time.monotonic() < deadline:
        status = _get(base, f"/api/jobs/{job_id}", token)["status"]
        if status != "queued":
            break
        time.sleep(0.2)
    return status


def bench_api(seconds: float = 15.0, clients: int = 4) -> dict[str, Any]:
    """Server memory, and latency idle / with a lab run in the API / on a worker."""
    from tests.e2e.stack import _server_env, build_stack, start_server, stop_server
    from tools.api_load import run_load

    token = "bench-" + os.urandom(8).hex()
    out: dict[str, Any] = {"clients": clients, "seconds": seconds}
    with workdir() as root:
        stack = build_stack(root / "stack")
        stack.env["STONKS_API_TOKEN"] = token
        start_server(stack)
        try:
            assert stack.process is not None
            pid = stack.process.pid
            out["api_rss_mb_start"] = _mb(rss_bytes(pid))
            idle = run_load(stack.base_url, token=token, clients=clients, seconds=seconds)
            out["idle"] = asdict(idle)
            out["api_rss_mb_after_load"] = _mb(rss_bytes(pid))
            job = _post(stack.base_url, "/api/lab/runs", token, _lab_body(stack))
            _wait_running(stack.base_url, token, job["id"])
            busy = run_load(stack.base_url, token=token, clients=clients, seconds=seconds)
            out["lab_in_api"] = asdict(busy)
            out["api_rss_mb_with_lab"] = _mb(rss_bytes(pid))
            with contextlib.suppress(Exception):
                _post(stack.base_url, f"/api/jobs/{job['id']}/cancel", token, {})
        finally:
            stop_server(stack)

        stack.env["STONKS_LAB_EXECUTOR"] = "worker"
        start_server(stack)
        worker = subprocess.Popen(
            [sys.executable, "-m", "stonks.lab.offload", "worker"],
            cwd=stack.root,
            env=_server_env(stack),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            job = _post(stack.base_url, "/api/lab/runs", token, _lab_body(stack))
            status = _wait_running(stack.base_url, token, job["id"])
            out["worker_job_status"] = status
            offloaded = run_load(stack.base_url, token=token, clients=clients, seconds=seconds)
            out["lab_on_worker"] = asdict(offloaded)
            out["worker_rss_mb"] = _mb(rss_bytes(worker.pid))
            with contextlib.suppress(Exception):
                _post(stack.base_url, f"/api/jobs/{job['id']}/cancel", token, {})
        finally:
            worker.terminate()
            try:
                worker.wait(timeout=30)
            except subprocess.TimeoutExpired:
                worker.kill()
            stop_server(stack)
    return out


def _mb(value: int | None) -> float | None:
    return None if value is None else round(value / MB, 1)


# ---- entry point ----------------------------------------------------------------------


def machine() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpus": os.cpu_count(),
        "processor": platform.processor(),
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def _ints(text: str) -> list[int]:
    return [int(x) for x in text.split(",") if x.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m tools.benchmark", description=__doc__)
    parser.add_argument(
        "section", choices=["storage", "tick", "lab", "api", "all"], help="what to measure"
    )
    parser.add_argument("--out", type=Path, default=None, help="also write JSON here")
    parser.add_argument("--tickers", type=int, default=50, help="storage: tickers")
    parser.add_argument("--years", type=int, default=5, help="storage: years of daily bars")
    parser.add_argument("--strategies", default="1,5,20", help="tick: active strategies")
    parser.add_argument("--portfolios", default="1,5,20", help="tick: portfolios")
    parser.add_argument("--ticks", type=int, default=4, help="tick: ticks timed per case")
    parser.add_argument("--presets", default="quick,standard,promotion", help="lab: presets")
    parser.add_argument("--budget", type=int, default=10, help="lab: tuning trials")
    parser.add_argument("--workers", type=int, default=1, help="lab: pool processes")
    parser.add_argument("--seconds", type=float, default=15.0, help="api: seconds per load")
    parser.add_argument("--clients", type=int, default=4, help="api: concurrent clients")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    from stonks.logging import configure_logging

    args = build_parser().parse_args(argv)
    configure_logging(level="ERROR")
    report: dict[str, Any] = {"machine": machine()}
    wanted = {"storage", "tick", "lab", "api"} if args.section == "all" else {args.section}
    if "storage" in wanted:
        report["storage"] = bench_storage(args.tickers, args.years)
    if "tick" in wanted:
        report["tick"] = bench_tick(
            _ints(args.strategies), _ints(args.portfolios), ticks=args.ticks
        )
    if "lab" in wanted:
        report["lab"] = bench_lab(
            [p for p in args.presets.split(",") if p], budget=args.budget, workers=args.workers
        )
    if "api" in wanted:
        report["api"] = bench_api(seconds=args.seconds, clients=args.clients)
    text = json.dumps(report, indent=2)
    print(text)
    if args.out is not None:
        args.out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
