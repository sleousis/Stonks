"""lab.parallel, BL-07 contract: settings, SeedSequence per-task seeds, no
nested pools, cooperative checkpoints, and universe-scoped read-only lake
snapshots for worker processes."""

from __future__ import annotations

import os
import pickle
import random
from datetime import date
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

from stonks.lab import parallel
from stonks.lab.dataset import LabDataset
from stonks.lab.parallel import (
    DatasetSpec,
    LakeSnapshot,
    ParallelSettings,
    dataset_snapshot,
    planned_workers,
    run_tasks,
    task_seeds,
)
from tests.fixtures.parallel_lab import random_walk_lake

# ---- module-level task functions (importable by spawned workers) -----------


def _rng_task(state, task):
    return task, random.random(), float(np.random.random())


def _nested_task(state, task):
    inner = run_tasks(_pid_task, [1, 2, 3], max_workers=2)
    return os.getpid(), inner


def _pid_task(state, task):
    return os.getpid()


def _env_task(state, task):
    return os.environ.get("OPENBLAS_NUM_THREADS"), os.environ.get("OMP_NUM_THREADS")


def _snapshot_task(dataset, ticker):
    lake = dataset.lake
    n = int(lake.sql("SELECT COUNT(*) AS n FROM bars WHERE ticker = ?", [ticker])["n"].iloc[0])
    try:
        lake.con.execute("DELETE FROM bars")
        wrote = True
    except duckdb.Error:
        wrote = False
    return ticker, n, lake.read_only, wrote, os.getpid()


def _open(spec):
    return spec.open() if isinstance(spec, DatasetSpec) else spec


# ---- settings ---------------------------------------------------------------


def test_zero_max_workers_means_all_cores_capped_at_32(monkeypatch):
    monkeypatch.setattr(parallel, "default_max_workers", lambda: 64)
    assert ParallelSettings().resolved_workers() == 32
    monkeypatch.setattr(parallel, "default_max_workers", lambda: 6)
    assert ParallelSettings(max_workers=0).resolved_workers() == 6
    assert ParallelSettings(max_workers=3).resolved_workers() == 3


def test_settings_validate():
    with pytest.raises(ValueError):
        ParallelSettings(max_workers=-1)
    with pytest.raises(ValueError):
        ParallelSettings(blas_threads=0)


def test_planned_workers_never_exceeds_the_task_count():
    assert planned_workers(3, settings=ParallelSettings(max_workers=8)) == 3
    assert planned_workers(0, settings=ParallelSettings(max_workers=8)) == 0
    assert planned_workers(10, max_workers=1) == 1


# ---- seeds ------------------------------------------------------------------


def test_task_seeds_come_from_seed_sequence_spawn():
    expected = [int(child.generate_state(1)[0]) for child in np.random.SeedSequence(42).spawn(5)]
    assert task_seeds(42, 5) == expected
    assert len(set(expected)) == 5
    # a prefix is stable when n grows: task i keeps its seed
    assert task_seeds(42, 8)[:5] == expected


@pytest.mark.parametrize("workers", [2, 3])
def test_root_seed_makes_global_rngs_identical_for_any_worker_count(workers):
    tasks = list(range(6))
    serial = run_tasks(_rng_task, tasks, root_seed=11, max_workers=1)
    pooled = run_tasks(_rng_task, tasks, root_seed=11, max_workers=workers)
    assert pooled == serial
    assert len({r for _, r, _ in serial}) == 6  # every task drew its own stream


def test_serial_seeding_restores_the_callers_global_rng_state():
    random.seed(123)
    np.random.seed(123)
    expected = (random.random(), float(np.random.random()))
    random.seed(123)
    np.random.seed(123)
    run_tasks(_rng_task, [1, 2], root_seed=5, max_workers=1)
    assert (random.random(), float(np.random.random())) == expected


# ---- pool behaviour ---------------------------------------------------------


def test_max_workers_one_never_starts_a_pool(monkeypatch):
    def _no_pool(*a, **kw):
        raise AssertionError("a process pool was started")

    monkeypatch.setattr(parallel, "ProcessPoolExecutor", _no_pool)
    assert run_tasks(_pid_task, [1, 2, 3], max_workers=1) == [os.getpid()] * 3
    assert (
        run_tasks(_pid_task, [1, 2], settings=ParallelSettings(max_workers=1)) == [os.getpid()] * 2
    )


def test_tasks_never_start_nested_pools():
    out = run_tasks(_nested_task, [1, 2], max_workers=2)
    for worker_pid, inner in out:
        assert worker_pid != os.getpid()
        assert inner == [worker_pid] * 3  # inner run stayed in the worker


def test_blas_threads_setting_reaches_workers(monkeypatch):
    for var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS"):
        monkeypatch.delenv(var, raising=False)
    out = run_tasks(_env_task, [1, 2], settings=ParallelSettings(max_workers=2, blas_threads=2))
    assert out == [("2", "2")] * 2
    assert "OPENBLAS_NUM_THREADS" not in os.environ  # parent env restored


def test_serial_checkpoint_runs_before_every_task_and_can_stop_the_run():
    calls: list[int] = []

    class Stop(Exception):
        pass

    def checkpoint():
        calls.append(len(calls))
        if len(calls) == 3:
            raise Stop

    done: list[int] = []

    def task(state, t):
        done.append(t)
        return t

    with pytest.raises(Stop):
        run_tasks(task, [1, 2, 3, 4], max_workers=1, checkpoint=checkpoint)
    assert done == [1, 2]


def test_parallel_checkpoint_runs_in_the_parent_and_propagates():
    calls: list[int] = []

    def checkpoint():
        calls.append(os.getpid())
        if len(calls) == 2:
            raise KeyboardInterrupt  # BaseException, like JobCancelled

    with pytest.raises(KeyboardInterrupt):
        run_tasks(_pid_task, list(range(6)), max_workers=2, checkpoint=checkpoint)
    assert set(calls) == {os.getpid()}


def test_parallel_checkpoint_passes_through_results_in_order():
    seen = []
    out = run_tasks(
        _rng_task, [5, 4, 3], max_workers=2, root_seed=1, checkpoint=lambda: seen.append(1)
    )
    assert [t for t, _, _ in out] == [5, 4, 3]
    assert len(seen) >= 3


# ---- snapshots --------------------------------------------------------------


@pytest.fixture
def mem_lake():
    lake = random_walk_lake(":memory:", ["A.US", "B.US", "C.US"], periods=40)
    yield lake
    lake.close()


def test_snapshot_materialises_an_in_memory_lake_to_a_universe_scoped_file(mem_lake):
    end = pd.bdate_range("2024-01-02", periods=40)[29].date()
    with LakeSnapshot.build(mem_lake, ["A.US", "B.US"], end=end) as snap:
        assert snap.path.is_file()
        with snap.open() as lake:
            assert lake.read_only
            bars = lake.sql(
                "SELECT ticker, MAX(timestamp) AS hi, COUNT(*) AS n FROM bars GROUP BY 1"
            )
            assert sorted(bars["ticker"]) == ["A.US", "B.US"]
            assert set(bars["n"]) == {30}
            assert all(pd.Timestamp(t).date() <= end for t in bars["hi"])
            assert sorted(lake.sql("SELECT id FROM instruments")["id"]) == ["A.US", "B.US"]
            assert len(lake.sql("SELECT * FROM prices")) == 60  # the view came along
    assert not snap.path.exists()
    assert not snap.path.parent.exists()


def test_snapshot_without_end_keeps_all_bars(mem_lake):
    with LakeSnapshot.build(mem_lake, ["C.US"]) as snap, snap.open() as lake:
        assert int(lake.sql("SELECT COUNT(*) AS n FROM bars")["n"].iloc[0]) == 40


def test_snapshot_needs_a_universe(mem_lake):
    with pytest.raises(ValueError):
        LakeSnapshot.build(mem_lake, [])


def test_snapshot_of_a_file_lake_does_not_lock_the_source(tmp_path):
    source = random_walk_lake(tmp_path / "lake.duckdb", ["A.US"], periods=10)
    try:
        with LakeSnapshot.build(source, ["A.US"]) as snap, snap.open():
            # the source stays writable while workers read the snapshot
            source.con.execute("DELETE FROM bars WHERE ticker = 'A.US'")
    finally:
        source.close()


def test_snapshot_close_is_idempotent(mem_lake):
    snap = LakeSnapshot.build(mem_lake, ["A.US"])
    snap.close()
    snap.close()
    assert not snap.path.exists()


def _dataset(lake) -> LabDataset:
    return LabDataset(
        lake=lake,
        universe=["A.US", "B.US"],
        start=date(2024, 1, 2),
        end=date(2024, 2, 1),
    )


def test_dataset_snapshot_yields_a_picklable_spec_that_reopens_read_only(mem_lake):
    ds = _dataset(mem_lake)
    with dataset_snapshot(ds) as spec:
        assert isinstance(spec, DatasetSpec)
        copy = pickle.loads(pickle.dumps(spec))
        reopened = copy.open()
        try:
            assert reopened.lake.read_only
            assert reopened.universe == ds.universe and reopened.end == ds.end
            assert reopened.train_window == ds.train_window
        finally:
            reopened.lake.close()
        path = spec.snapshot_path
    assert not Path(path).exists()
    assert ds.lake is mem_lake  # the caller's dataset is untouched


def test_dataset_snapshot_passes_through_datasets_without_a_lake():
    marker = object()
    with dataset_snapshot(marker) as same:
        assert same is marker


def test_workers_each_open_one_read_only_lake_on_the_snapshot(mem_lake):
    with dataset_snapshot(_dataset(mem_lake)) as spec:
        out = run_tasks(
            _snapshot_task, ["A.US", "B.US", "C.US"], setup=_open, payload=spec, max_workers=2
        )
    assert [(t, n) for t, n, *_ in out] == [("A.US", 23), ("B.US", 23), ("C.US", 0)]
    assert all(ro and not wrote for _, _, ro, wrote, _ in out)
    assert os.getpid() not in {pid for *_, pid in out}
