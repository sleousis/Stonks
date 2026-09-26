"""Process-pool helper for survival tests that run many independent
backtests (permutations, folds).

:func:`run_tasks` is an ordered map over a ``spawn`` process pool with
per-worker state: ``setup(payload)`` runs once in each worker (for example
to open that worker's own DuckDB connection) and ``task_fn(state, task)``
runs once per task. Results come back in task order. With
``max_workers=1`` (or fewer than two tasks) the same two functions run in
this process, with ``payload`` never pickled — that is the serial path the
parallel one must match.

Determinism is the caller's job and is easy to keep: draw every random
seed in the parent, before dispatch, and put it in the task. A task must
then depend only on ``(state, task)``, never on which worker ran it or in
what order.

Handles carry live objects into workers without making them picklable in
general: in this process they hand back the object itself; pickled (only
when a pool is used) they serialize a portable copy, once, and rebuild it
in the worker.

- :class:`StrategyHandle` — a strategy, via its own ``save`` / ``load``
  (the registry's round-trip contract, so fitted state comes along).
- :class:`LakeHandle` — a lake's universe tables (everything but
  ``bars``, see ``lab.lake_copy``), rebuilt in the worker as a private
  in-memory lake: one DuckDB connection per worker, none shared.

``max_workers=None`` means :func:`default_max_workers`: the
``STONKS_LAB_MAX_WORKERS`` environment variable when set, else
``os.cpu_count()``.
"""

from __future__ import annotations

import multiprocessing
import os
import shutil
import tempfile
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any

import pandas as pd

from stonks.core.protocols import Strategy
from stonks.lab.lake_copy import copy_universe_lake
from stonks.store.lake import DuckDBLake

MAX_WORKERS_ENV = "STONKS_LAB_MAX_WORKERS"

# Per-worker state set by ``_init_worker`` (one value per worker process).
_WORKER_STATE: Any = None


def default_max_workers() -> int:
    """``STONKS_LAB_MAX_WORKERS`` if set, else the machine's CPU count."""
    env = os.environ.get(MAX_WORKERS_ENV)
    if env:
        return max(1, int(env))
    return os.cpu_count() or 1


def resolve_max_workers(max_workers: int | None) -> int:
    if max_workers is None:
        return default_max_workers()
    if max_workers < 1:
        raise ValueError("max_workers must be >= 1")
    return max_workers


def run_tasks[T, R](
    task_fn: Callable[[Any, T], R],
    tasks: Sequence[T],
    *,
    setup: Callable[[Any], Any] | None = None,
    payload: Any = None,
    max_workers: int | None = None,
) -> list[R]:
    """``[task_fn(state, t) for t in tasks]`` with ``state = setup(payload)``
    built once per worker (``payload`` itself when ``setup`` is None).

    ``task_fn`` and ``setup`` must be module-level functions and, in the
    parallel case, ``payload``, the tasks and the results picklable.
    Exceptions raised by a task propagate."""
    workers = min(resolve_max_workers(max_workers), len(tasks))
    if workers <= 1:
        state = setup(payload) if setup is not None else payload
        return [task_fn(state, task) for task in tasks]
    with ProcessPoolExecutor(
        max_workers=workers,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=_init_worker,
        initargs=(setup, payload),
    ) as pool:
        return list(pool.map(partial(_call, task_fn), tasks))


def _init_worker(setup: Callable[[Any], Any] | None, payload: Any) -> None:
    global _WORKER_STATE
    _WORKER_STATE = setup(payload) if setup is not None else payload


def _call(task_fn: Callable[[Any, Any], Any], task: Any) -> Any:
    return task_fn(_WORKER_STATE, task)


# ---- handles ----------------------------------------------------------------


class StrategyHandle:
    """A strategy that can cross into a worker process (see module doc)."""

    def __init__(self, strategy: Strategy) -> None:
        self.strategy = strategy
        self._portable: tuple[type, dict[str, bytes]] | None = None

    def __getstate__(self) -> dict[str, Any]:
        if self._portable is None:
            tmp = Path(tempfile.mkdtemp(prefix="stonks-strategy-"))
            try:
                self.strategy.save(tmp)
                files = {
                    p.relative_to(tmp).as_posix(): p.read_bytes()
                    for p in sorted(tmp.rglob("*"))
                    if p.is_file()
                }
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            self._portable = (type(self.strategy), files)
        return {"portable": self._portable}

    def __setstate__(self, state: dict[str, Any]) -> None:
        cls, files = state["portable"]
        tmp = Path(tempfile.mkdtemp(prefix="stonks-strategy-"))
        try:
            for rel, data in files.items():
                target = tmp / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            self.strategy = cls.load(tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self._portable = state["portable"]


class LakeHandle:
    """A lake's ``universe`` tables (no ``bars``) that can cross into a
    worker process, where they become a private in-memory lake."""

    def __init__(self, lake: DuckDBLake, universe: Sequence[str]) -> None:
        if not universe:
            raise ValueError("LakeHandle needs a non-empty universe")
        self.lake = lake
        self.universe = list(universe)
        self._owned = False
        self._frames: dict[str, pd.DataFrame] | None = None

    def close(self) -> None:
        """Close the lake if this handle built it (in a worker)."""
        if self._owned:
            self.lake.close()

    def __getstate__(self) -> dict[str, Any]:
        if self._frames is None:
            with copy_universe_lake(self.lake, self.universe) as snapshot:
                self._frames = {
                    table: snapshot.con.execute(f'SELECT * FROM "{table}"').fetchdf()
                    for table in _base_tables(snapshot)
                }
            self._frames = {t: f for t, f in self._frames.items() if not f.empty}
        return {"universe": self.universe, "frames": self._frames}

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.universe = state["universe"]
        self._frames = state["frames"]
        lake = DuckDBLake(Path(":memory:"))
        try:
            lake.migrate()
            for table, frame in self._frames.items():
                cols = ", ".join(f'"{c}"' for c in frame.columns)
                lake.con.register("_handle_src", frame)
                try:
                    lake.con.execute(
                        f'INSERT INTO "{table}" ({cols}) SELECT {cols} FROM _handle_src'
                    )
                finally:
                    lake.con.unregister("_handle_src")
        except Exception:
            lake.close()
            raise
        self.lake = lake
        self._owned = True


def _base_tables(lake: DuckDBLake) -> list[str]:
    rows = lake.con.execute(
        "SELECT table_name FROM information_schema.tables"
        " WHERE table_schema = 'main' AND table_type = 'BASE TABLE' ORDER BY table_name"
    ).fetchall()
    return [r[0] for r in rows if r[0] not in {"schema_migrations", "bars"}]
