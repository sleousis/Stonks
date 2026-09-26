"""Background job store + in-process runner."""

from __future__ import annotations

import math
import threading
import time

import pytest
from pydantic import BaseModel

from stonks.app.errors import ConflictError, NotFoundError, ValidationError
from stonks.app.jobs import JobContext, JobRunner, JobStore
from stonks.store.state import SqliteState


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "state.sqlite"
    with SqliteState(path) as s:
        s.migrate()
    return JobStore(path)


@pytest.fixture
def runner(store):
    r = JobRunner(store, max_workers=2)
    yield r
    r.shutdown()


def test_migration_creates_jobs_table(tmp_path):
    with SqliteState(tmp_path / "s.sqlite") as s:
        s.migrate()
        assert "jobs" in s.tables()
        assert 3 in s.applied_migrations()


def test_create_and_get_job(store):
    job = store.create("demo", {"x": 1})
    assert job.status == "queued"
    assert job.progress == 0.0
    fetched = store.get(job.id)
    assert fetched.params == {"x": 1}
    assert fetched.kind == "demo"
    assert fetched.created_at is not None


def test_get_unknown_job_raises_not_found(store):
    with pytest.raises(NotFoundError):
        store.get("nope")


class _Out(BaseModel):
    value: float
    ratio: float


def test_runner_runs_handler_and_records_result(runner):
    def handler(params: dict, ctx: JobContext) -> _Out:
        ctx.progress(0.5, "halfway")
        return _Out(value=params["x"] * 2, ratio=math.inf)

    runner.register("double", handler)
    job = runner.submit("double", {"x": 21})
    done = runner.wait(job.id, timeout=10)
    assert done.status == "succeeded"
    assert done.progress == 1.0
    # non-finite floats can't be JSON; they land as null.
    assert done.result == {"value": 42.0, "ratio": None}
    assert done.started_at is not None
    assert done.finished_at is not None
    assert done.error is None


def test_runner_marks_crashed_job_failed(runner):
    def boom(params: dict, ctx: JobContext) -> None:
        raise RuntimeError("kaboom")

    runner.register("boom", boom)
    done = runner.wait(runner.submit("boom", {}).id, timeout=10)
    assert done.status == "failed"
    assert "kaboom" in (done.error or "")
    assert done.finished_at is not None

    # the runner survives and runs the next job
    runner.register("ok", lambda p, c: {"fine": True})
    assert runner.wait(runner.submit("ok", {}).id, timeout=10).status == "succeeded"


def test_submit_unknown_kind_is_validation_error(runner):
    with pytest.raises(ValidationError):
        runner.submit("unknown-kind", {})


def test_cancel_queued_job_never_runs(store):
    runner = JobRunner(store, max_workers=1)
    gate = threading.Event()
    ran: list[str] = []

    def blocker(params: dict, ctx: JobContext) -> None:
        gate.wait(10)
        ran.append(params["name"])

    runner.register("block", blocker)
    try:
        first = runner.submit("block", {"name": "first"})
        _wait_for_status(store, first.id, "running")
        second = runner.submit("block", {"name": "second"})
        cancelled = store.cancel(second.id)
        assert cancelled.status == "cancelled"
        gate.set()
        assert runner.wait(first.id, timeout=10).status == "succeeded"
        assert runner.wait(second.id, timeout=10).status == "cancelled"
        assert ran == ["first"]
    finally:
        gate.set()
        runner.shutdown()


def test_cancel_running_job_is_conflict(store):
    runner = JobRunner(store, max_workers=1)
    gate = threading.Event()
    runner.register("block", lambda p, c: gate.wait(10))
    try:
        job = runner.submit("block", {})
        _wait_for_status(store, job.id, "running")
        with pytest.raises(ConflictError):
            store.cancel(job.id)
    finally:
        gate.set()
        runner.shutdown()


def test_cancel_finished_job_is_conflict(runner, store):
    runner.register("ok", lambda p, c: None)
    job = runner.wait(runner.submit("ok", {}).id, timeout=10)
    with pytest.raises(ConflictError):
        store.cancel(job.id)


def test_recover_interrupted_marks_unfinished_jobs_failed(store):
    queued = store.create("demo", {})
    running = store.create("demo", {})
    assert store.claim(running.id)
    finished = store.create("demo", {})
    store.claim(finished.id)
    store.finish(finished.id, "succeeded", result={"ok": 1})

    assert store.recover_interrupted() == 2
    for jid in (queued.id, running.id):
        job = store.get(jid)
        assert job.status == "failed"
        assert "interrupted" in (job.error or "")
        assert job.finished_at is not None
    assert store.get(finished.id).status == "succeeded"


def test_bounded_concurrency(store):
    runner = JobRunner(store, max_workers=2)
    lock = threading.Lock()
    active = 0
    peak = 0

    def work(params: dict, ctx: JobContext) -> None:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.15)
        with lock:
            active -= 1

    runner.register("work", work)
    try:
        ids = [runner.submit("work", {}).id for _ in range(5)]
        for jid in ids:
            assert runner.wait(jid, timeout=20).status == "succeeded"
    finally:
        runner.shutdown()
    assert peak == 2


def test_jobs_sharing_a_lock_never_overlap(store):
    runner = JobRunner(store, max_workers=4)
    guard = threading.Lock()
    active = 0
    peak = 0

    def work(params: dict, ctx: JobContext) -> None:
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(0.1)
        with guard:
            active -= 1

    runner.register("writer", work, lock="lake_write")
    try:
        ids = [runner.submit("writer", {}).id for _ in range(3)]
        for jid in ids:
            assert runner.wait(jid, timeout=20).status == "succeeded"
    finally:
        runner.shutdown()
    assert peak == 1


def test_list_jobs_filters_and_paginates(store):
    for i in range(5):
        store.create("a" if i % 2 == 0 else "b", {"i": i})
    page = store.list(limit=2, offset=0)
    assert page.total == 5
    assert len(page.items) == 2
    # newest first
    assert page.items[0].params == {"i": 4}
    only_b = store.list(kind="b", limit=10, offset=0)
    assert only_b.total == 2
    assert {j.kind for j in only_b.items} == {"b"}
    queued = store.list(status="queued", limit=10, offset=3)
    assert queued.total == 5
    assert len(queued.items) == 2


def _wait_for_status(store: JobStore, job_id: str, status: str, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if store.get(job_id).status == status:
            return
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} never reached {status}")
