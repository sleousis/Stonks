"""The one process pool in Stonks: tuning trials, survival-test
permutations and folds all fan out through :func:`run_tasks`.

:func:`run_tasks` is an ordered map over a ``spawn`` process pool with
per-worker state: ``setup(payload)`` runs once in each worker (for example
to open that worker's own DuckDB connection) and ``task_fn(state, task)``
runs once per task. Results come back in task order. With
``max_workers=1`` (or fewer than two tasks) the same two functions run in
this process, with ``payload`` never pickled — that is the serial path the
parallel one must match.

Determinism (output identical for any worker count):

- results always come back in task order;
- every random seed is drawn in the parent, before dispatch: either put
  it in the task yourself, or pass ``root_seed`` and task ``i`` gets
  ``task_seeds(root_seed, n)[i]`` (``numpy.random.SeedSequence(root_seed)
  .spawn(n)[i]``), which also seeds the global ``random`` / ``numpy.random``
  state for that task (restored afterwards in-process), so even code that
  uses the global RNGs is reproducible;
- a task must depend only on ``(state, task)``, never on which worker ran
  it or in what order.

Nested pools are forbidden: inside a worker, :func:`run_tasks` always
runs in-process (a task that tunes runs its inner tuner serially).

``checkpoint`` (a callable run in the parent before each task in-process,
and after each result on the pool path) is the cooperative-cancellation
hook: whatever it raises stops the run, pending tasks are cancelled and
the exception propagates.

Lake access from workers goes through a :class:`LakeSnapshot`: a
universe- and window-scoped DuckDB file built once per run in a temp
directory, opened ``read_only`` by each worker (one connection per
worker), so workers never contend with the API or ingest for the lake's
write lock, and the data behind the run cannot change under it. A
snapshot lives only as long as the run that built it (never reused, so
never stale) and is deleted after the pool has shut down, when no worker
holds the file any more (Windows cannot delete open files). When the
source keeps its bars in Parquet (roadmap 10.4) the snapshot file holds
everything but the bars, and the universe's bar partitions are hard-linked
(copied across volumes) into ``<snapshot dir>/bars``: workers read the
files directly, and since writers replace files rather than modify them,
the run's bars still cannot change under it.
:func:`dataset_snapshot` wraps a ``LabDataset`` into a picklable
:class:`DatasetSpec` over such a snapshot.

Handles carry live objects into workers without making them picklable in
general: in this process they hand back the object itself; pickled (only
when a pool is used) they serialize a portable copy, once, and rebuild it
in the worker.

- :class:`PortableStrategy` — a strategy, via its own ``save`` / ``load``
  (the registry's round-trip contract, so fitted state comes along).
- :class:`PortableLake` — a lake's universe tables (everything but
  ``bars``, see ``lab.lake_copy``), rebuilt in the worker as a private
  in-memory lake: one DuckDB connection per worker, none shared.

``max_workers=None`` means :func:`default_max_workers`: the
``STONKS_LAB_MAX_WORKERS`` environment variable when set, else
``os.cpu_count()``. :class:`ParallelSettings` is the settings-model form
(``max_workers=0`` for all cores, capped at :data:`MAX_AUTO_WORKERS`).

Workers start with single-threaded BLAS/OpenMP (``OPENBLAS_NUM_THREADS``
etc. set to ``ParallelSettings.blas_threads``, default 1, unless the user
set them): the pool is the parallelism, N
workers x N BLAS threads would oversubscribe the CPU, and OpenBLAS commits
per-thread buffers at import (~0.8 GB per process on a 32-core machine,
enough to exhaust the commit limit with 32 workers).
"""

from __future__ import annotations

import dataclasses
import multiprocessing
import os
import random
import shutil
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import Future, ProcessPoolExecutor
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from stonks.core.protocols import Strategy
from stonks.lab.lake_copy import copy_universe_lake
from stonks.logging import get_logger
from stonks.store.bars import ParquetBarStore
from stonks.store.lake import DuckDBLake

_log = get_logger("stonks.lab.parallel")

MAX_WORKERS_ENV = "STONKS_LAB_MAX_WORKERS"

#: Cap on ``ParallelSettings(max_workers=0)``: past this, process start-up
#: and snapshot opening cost more than the extra workers bring.
MAX_AUTO_WORKERS = 32

#: Thread-count variables pinned in workers (unless already set).
_THREAD_ENV = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")

# Per-worker state set by ``_init_worker`` (one value per worker process).
_WORKER_STATE: Any = None
# True in pool workers: ``run_tasks`` then never starts a nested pool.
_IN_WORKER = False


class ParallelSettings(BaseModel):
    """How lab work spreads over processes (``[lab.parallel]``)."""

    model_config = ConfigDict(frozen=True)

    #: Worker processes; 0 means every core (:func:`default_max_workers`,
    #: capped at :data:`MAX_AUTO_WORKERS`), 1 runs in-process.
    max_workers: int = Field(default=0, ge=0)
    #: BLAS/OpenMP threads per worker.
    blas_threads: int = Field(default=1, ge=1)

    def resolved_workers(self) -> int:
        if self.max_workers == 0:
            return min(MAX_AUTO_WORKERS, default_max_workers())
        return self.max_workers


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


def planned_workers(
    n_tasks: int, *, max_workers: int | None = None, settings: ParallelSettings | None = None
) -> int:
    """How many processes :func:`run_tasks` will use for ``n_tasks`` tasks
    with these options (1 inside a worker: pools never nest). Callers use
    it to skip pool-only preparation, such as a lake snapshot, when the
    run will stay in-process."""
    if max_workers is None and settings is not None:
        requested = settings.resolved_workers()
    else:
        requested = resolve_max_workers(max_workers)
    if _IN_WORKER:
        requested = 1
    return min(requested, n_tasks)


def task_seeds(root_seed: int, n: int) -> list[int]:
    """Seed of each of ``n`` tasks: ``SeedSequence(root_seed).spawn(n)[i]``
    as a 32-bit int. Task ``i``'s seed does not depend on ``n``."""
    return [int(child.generate_state(1)[0]) for child in np.random.SeedSequence(root_seed).spawn(n)]


def run_tasks[T, R](
    task_fn: Callable[[Any, T], R],
    tasks: Sequence[T],
    *,
    setup: Callable[[Any], Any] | None = None,
    payload: Any = None,
    max_workers: int | None = None,
    settings: ParallelSettings | None = None,
    root_seed: int | None = None,
    checkpoint: Callable[[], None] | None = None,
) -> list[R]:
    """``[task_fn(state, t) for t in tasks]`` with ``state = setup(payload)``
    built once per worker (``payload`` itself when ``setup`` is None).

    ``max_workers`` wins over ``settings``; with neither, every core. With
    ``root_seed`` each task runs with the global RNGs seeded from
    :func:`task_seeds`. ``checkpoint`` is the cancellation hook (see the
    module doc).

    ``task_fn`` and ``setup`` must be module-level functions and, in the
    parallel case, ``payload``, the tasks and the results picklable.
    Exceptions raised by a task propagate."""
    tasks = list(tasks)
    workers = planned_workers(len(tasks), max_workers=max_workers, settings=settings)
    seeds: list[int | None] = (
        list(task_seeds(root_seed, len(tasks))) if root_seed is not None else [None] * len(tasks)
    )
    blas = settings.blas_threads if settings is not None else 1
    if workers <= 1:
        with _blas_limit(blas):
            state = setup(payload) if setup is not None else payload
            results: list[R] = []
            for task, seed in zip(tasks, seeds, strict=True):
                if checkpoint is not None:
                    checkpoint()
                results.append(_call_seeded(task_fn, state, task, seed))
        return results
    with (
        _pinned_thread_env(blas),
        ProcessPoolExecutor(
            max_workers=workers,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=_init_worker,
            initargs=(setup, payload),
        ) as pool,
    ):
        futures: list[Future[R]] = [
            pool.submit(_call, task_fn, task, seed) for task, seed in zip(tasks, seeds, strict=True)
        ]
        try:
            out: list[R] = []
            for future in futures:
                out.append(future.result())
                if checkpoint is not None:
                    checkpoint()
            return out
        except BaseException:
            for future in futures:
                future.cancel()
            raise


@contextmanager
def _pinned_thread_env(threads: int) -> Iterator[None]:
    """Set the unset ``_THREAD_ENV`` variables to ``threads`` while the
    pool spawns its workers (children inherit the environment; numpy reads
    it at import), then restore the parent's environment."""
    added = [v for v in _THREAD_ENV if v not in os.environ]
    for var in added:
        os.environ[var] = str(threads)
    try:
        yield
    finally:
        for var in added:
            os.environ.pop(var, None)


@contextmanager
def _blas_limit(threads: int) -> Iterator[None]:
    """Limit the already-loaded BLAS/OpenMP pools of this process to
    ``threads`` for the block, so the in-process path computes exactly
    like a worker (multi-threaded BLAS reductions can differ in the last
    bits). A no-op when ``threadpoolctl`` (a scikit-learn dependency) is
    missing."""
    try:
        from threadpoolctl import threadpool_limits
    except ImportError:  # pragma: no cover - installed with scikit-learn
        yield
        return
    with threadpool_limits(limits=threads):
        yield


def _init_worker(setup: Callable[[Any], Any] | None, payload: Any) -> None:
    global _WORKER_STATE, _IN_WORKER
    _IN_WORKER = True
    _WORKER_STATE = setup(payload) if setup is not None else payload


def _call(task_fn: Callable[[Any, Any], Any], task: Any, seed: int | None) -> Any:
    return _call_seeded(task_fn, _WORKER_STATE, task, seed)


def _call_seeded(
    task_fn: Callable[[Any, Any], Any], state: Any, task: Any, seed: int | None
) -> Any:
    """``task_fn(state, task)``, with the global RNGs seeded from ``seed``
    for the call and restored after it (when ``seed`` is not None)."""
    if seed is None:
        return task_fn(state, task)
    py_state = random.getstate()
    np_state = np.random.get_state()
    random.seed(seed)
    np.random.seed(seed)
    try:
        return task_fn(state, task)
    finally:
        random.setstate(py_state)
        np.random.set_state(np_state)


# ---- lake snapshots ---------------------------------------------------------


class LakeSnapshot:
    """A universe-scoped copy of a lake in a DuckDB file of its own, for
    worker processes to open read-only (see module doc).

    Holds every table a strategy may read, filtered to the universe (see
    ``lab.lake_copy``), plus the universe's bars at every interval up to
    ``end`` (all of them when ``end`` is None): in the file for a
    table-backed source, as Parquet partitions beside it for a Parquet
    one. Use as a context manager, or call :meth:`close`, which deletes
    the snapshot directory."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @classmethod
    def build(
        cls, source: DuckDBLake, universe: Sequence[str], *, end: date | None = None
    ) -> LakeSnapshot:
        tickers = list(universe)
        if not tickers:
            raise ValueError("a lake snapshot needs a non-empty universe")
        started = time.perf_counter()
        _sweep_stale_snapshots()
        directory = Path(tempfile.mkdtemp(prefix=_SNAPSHOT_PREFIX))
        snapshot = cls(directory / "snapshot.duckdb")
        try:
            with copy_universe_lake(source, tickers) as scoped:
                if isinstance(source.bar_store, ParquetBarStore):
                    # Workers read the universe's bar partitions directly:
                    # hard-linked (or copied) next to the snapshot file, so
                    # later writes to the source never reach the run.
                    scoped.export_database(snapshot.path, bar_backend="parquet")
                    source.bar_store.export_partitions(
                        snapshot.path.parent / "bars", tickers=tickers, end=end
                    )
                else:
                    _copy_bars(source, scoped, tickers, end)
                    scoped.export_database(snapshot.path)
        except BaseException:
            snapshot.close()
            raise
        _log.info(
            "lab.snapshot.built",
            tickers=len(tickers),
            end=str(end) if end is not None else None,
            seconds=round(time.perf_counter() - started, 3),
        )
        return snapshot

    def open(self) -> DuckDBLake:
        """A new read-only connection to the snapshot."""
        return DuckDBLake(self.path, read_only=True)

    def close(self) -> None:
        """Delete the snapshot directory. Retries briefly: on Windows a
        worker that just exited may release its handle a moment late."""
        directory = self.path.parent
        for attempt in range(20):
            if not directory.exists():
                return
            try:
                shutil.rmtree(directory)
                return
            except OSError:
                if attempt == 19:
                    _log.warning("lab.snapshot.cleanup_failed", path=str(directory))
                    return
                time.sleep(0.05 * (attempt + 1))

    def __enter__(self) -> LakeSnapshot:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


_SNAPSHOT_PREFIX = "stonks-snapshot-"
#: Snapshot directories older than this are leftovers of a killed run
#: (a live run's snapshot is younger than any lab run is long).
_STALE_SNAPSHOT_SECONDS = 86_400


def _sweep_stale_snapshots() -> None:
    """Delete snapshot directories a hard-killed run left in the temp dir.
    Best effort: one still open elsewhere is skipped."""
    cutoff = time.time() - _STALE_SNAPSHOT_SECONDS
    for directory in Path(tempfile.gettempdir()).glob(f"{_SNAPSHOT_PREFIX}*"):
        try:
            if directory.is_dir() and directory.stat().st_mtime < cutoff:
                shutil.rmtree(directory)
                _log.info("lab.snapshot.stale_removed", path=str(directory))
        except OSError:
            continue


def _copy_bars(
    source: DuckDBLake, target: DuckDBLake, tickers: list[str], end: date | None
) -> None:
    query = "SELECT * FROM bars WHERE ticker = ANY(?)"
    params: list[Any] = [tickers]
    if end is not None:
        # bar timestamps are naive UTC; everything before the next midnight
        query += " AND timestamp < ?"
        params.append(end + timedelta(days=1))
    frame = source.con.execute(query, params).fetchdf()
    if frame.empty:
        return
    cols = ", ".join(f'"{c}"' for c in frame.columns)
    target.con.register("_snapshot_bars", frame)
    try:
        target.con.execute(f"INSERT INTO bars ({cols}) SELECT {cols} FROM _snapshot_bars")
    finally:
        target.con.unregister("_snapshot_bars")


@dataclasses.dataclass(frozen=True)
class DatasetSpec:
    """A picklable stand-in for a dataset: the dataset with its lake
    detached, plus the snapshot file a worker reopens it on."""

    dataset: Any
    snapshot_path: Path

    def open(self) -> Any:
        """The dataset on a new read-only connection to the snapshot (the
        caller closes ``.lake``; worker processes keep it for life)."""
        lake = DuckDBLake(self.snapshot_path, read_only=True)
        lake.con.execute("SET threads = 1")
        return dataclasses.replace(self.dataset, lake=lake)


@contextmanager
def dataset_snapshot(dataset: Any) -> Iterator[Any]:
    """Yield what to ship to workers for ``dataset``: a :class:`DatasetSpec`
    over a fresh :class:`LakeSnapshot` (universe, bars up to
    ``dataset.end``) when ``dataset`` is a dataclass holding a
    ``DuckDBLake``; ``dataset`` itself otherwise. The snapshot is deleted
    on exit, so leave the block only after the pool has shut down."""
    lake = getattr(dataset, "lake", None)
    if not (dataclasses.is_dataclass(dataset) and isinstance(lake, DuckDBLake)):
        yield dataset
        return
    end = getattr(dataset, "end", None)
    with LakeSnapshot.build(lake, list(dataset.universe), end=end) as snapshot:
        yield DatasetSpec(dataclasses.replace(dataset, lake=None), snapshot.path)


# ---- handles ----------------------------------------------------------------


class PortableStrategy:
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


class PortableLake:
    """A lake's ``universe`` tables (no ``bars``) that can cross into a
    worker process, where they become a private in-memory lake."""

    def __init__(self, lake: DuckDBLake, universe: Sequence[str]) -> None:
        if not universe:
            raise ValueError("PortableLake needs a non-empty universe")
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
    return [r[0] for r in rows if r[0] not in {"schema_migrations", "lake_settings", "bars"}]
