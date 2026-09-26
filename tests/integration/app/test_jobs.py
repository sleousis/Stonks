"""Background job store + in-process runner."""

from __future__ import annotations

import math
import sqlite3
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
    """Events, not sleeps (TT-07): the first two jobs hold their workers
    until the test has seen both running and the rest still queued."""
    runner = JobRunner(store, max_workers=2)
    lock = threading.Lock()
    two_running = threading.Event()
    release = threading.Event()
    active = 0
    peak = 0

    def work(params: dict, ctx: JobContext) -> None:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 2:
                two_running.set()
        release.wait(20)
        with lock:
            active -= 1

    runner.register("work", work)
    try:
        ids = [runner.submit("work", {}).id for _ in range(5)]
        assert two_running.wait(20)
        assert sorted(store.get(j).status for j in ids) == ["queued"] * 3 + ["running"] * 2
        release.set()
        for jid in ids:
            assert runner.wait(jid, timeout=20).status == "succeeded"
    finally:
        release.set()
        runner.shutdown()
    assert peak == 2


def test_jobs_sharing_a_lock_never_overlap(store):
    """The first writer holds the lock until the test has seen the other
    two still queued behind it (TT-07: no sleeps)."""
    runner = JobRunner(store, max_workers=4)
    guard = threading.Lock()
    release = threading.Event()
    active = 0
    peak = 0

    def work(params: dict, ctx: JobContext) -> None:
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        release.wait(20)
        with guard:
            active -= 1

    runner.register("writer", work, lock="lake_write")
    try:
        ids = [runner.submit("writer", {}).id for _ in range(3)]
        _wait_for_status(store, ids[0], "running")
        assert [store.get(j).status for j in ids[1:]] == ["queued", "queued"]
        release.set()
        for jid in ids:
            assert runner.wait(jid, timeout=20).status == "succeeded"
    finally:
        release.set()
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


def test_job_errors_are_scrubbed_of_credentials(store):
    runner = JobRunner(store, max_workers=1, secrets=lambda: ["hunter2"])

    def leaky(params: dict, ctx: JobContext) -> None:
        raise RuntimeError("GET https://vendor.test/eod?api_token=abc123 failed; key hunter2")

    runner.register("leaky", leaky)
    try:
        done = runner.wait(runner.submit("leaky", {}).id, timeout=10)
    finally:
        runner.shutdown()
    assert done.status == "failed"
    assert "abc123" not in done.error
    assert "hunter2" not in done.error
    assert "RuntimeError" in done.error


def test_job_waiting_on_lock_at_shutdown_is_cancelled_not_run(store):
    runner = JobRunner(store, max_workers=2)
    gate = threading.Event()
    ran: list[str] = []

    def work(params: dict, ctx: JobContext) -> None:
        if params["name"] == "first":
            gate.wait(10)
        ran.append(params["name"])

    runner.register("writer", work, lock="lake_write")
    first = runner.submit("writer", {"name": "first"})
    _wait_for_status(store, first.id, "running")
    second = runner.submit("writer", {"name": "second"})
    # The lock's lane has one worker, held by the first job: the second waits
    # in the lane's queue (no sleep needed, TT-07).
    assert store.get(second.id).status == "queued"
    runner.shutdown(wait=False)
    gate.set()
    _wait_for_status(store, first.id, "succeeded")
    _wait_for_status(store, second.id, "cancelled")
    assert ran == ["first"]


# ---- scheduling: locks never occupy general workers --------------------------


def test_tick_is_never_starved_by_lake_writers_or_lab_runs(store):
    """Two ingests (one running, one waiting on ``lake_write``) plus lab runs
    filling the general pool must not delay a tick."""
    runner = JobRunner(store, max_workers=2)
    gate = threading.Event()
    runner.register("ingest", lambda p, c: gate.wait(10), lock="lake_write")
    runner.register("lab", lambda p, c: gate.wait(10))
    runner.register("tick", lambda p, c: {"ticked": True}, lock="tick")
    try:
        ingests = [runner.submit("ingest", {}).id for _ in range(2)]
        labs = [runner.submit("lab", {}).id for _ in range(2)]
        _wait_for_status(store, ingests[0], "running")
        for jid in labs:
            _wait_for_status(store, jid, "running")
        tick = runner.submit("tick", {})
        done = runner.wait(tick.id, timeout=5)
        assert done.status == "succeeded"
        # the second ingest is still queued behind the first, not holding a worker
        assert store.get(ingests[1]).status == "queued"
    finally:
        gate.set()
        runner.shutdown()


def test_lake_writer_waiting_on_lock_leaves_general_pool_free(store):
    runner = JobRunner(store, max_workers=1)
    gate = threading.Event()
    runner.register("ingest", lambda p, c: gate.wait(10), lock="lake_write")
    runner.register("backtest", lambda p, c: "ok")
    try:
        first = runner.submit("ingest", {})
        runner.submit("ingest", {})
        _wait_for_status(store, first.id, "running")
        assert runner.wait(runner.submit("backtest", {}).id, timeout=5).status == "succeeded"
    finally:
        gate.set()
        runner.shutdown()


# ---- store failures: last-resort handling ----------------------------------


class _FlakyStore(JobStore):
    """Raises ``database is locked`` on the first N calls of chosen methods."""

    def __init__(self, path, failures: dict[str, int]) -> None:
        super().__init__(path)
        self.failures = dict(failures)

    def _maybe_fail(self, name: str) -> None:
        if self.failures.get(name, 0) > 0:
            self.failures[name] -= 1
            raise sqlite3.OperationalError("database is locked")

    def claim(self, job_id):
        self._maybe_fail("claim")
        return super().claim(job_id)

    def finish(self, job_id, status, **kw):
        self._maybe_fail("finish")
        return super().finish(job_id, status, **kw)

    def set_progress(self, job_id, progress, message=None):
        self._maybe_fail("set_progress")
        return super().set_progress(job_id, progress, message)


def _flaky(store: JobStore, **failures: int) -> _FlakyStore:
    return _FlakyStore(store._path, failures)


def test_finish_is_retried_when_the_database_is_locked(store):
    flaky = _flaky(store, finish=2)
    runner = JobRunner(flaky, max_workers=1, retry_delays=(0.0, 0.0, 0.0))
    runner.register("ok", lambda p, c: {"v": 1})
    try:
        done = runner.wait(runner.submit("ok", {}).id, timeout=10)
    finally:
        runner.shutdown()
    assert done.status == "succeeded"
    assert done.result == {"v": 1}


def test_claim_is_retried_when_the_database_is_locked(store):
    flaky = _flaky(store, claim=1)
    runner = JobRunner(flaky, max_workers=1, retry_delays=(0.0, 0.0))
    runner.register("ok", lambda p, c: "fine")
    try:
        done = runner.wait(runner.submit("ok", {}).id, timeout=10)
    finally:
        runner.shutdown()
    assert done.status == "succeeded"


def test_progress_write_failure_does_not_fail_the_job(store):
    flaky = _flaky(store, set_progress=5)

    def handler(params: dict, ctx: JobContext) -> str:
        ctx.progress(0.5, "halfway")
        return "done"

    runner = JobRunner(flaky, max_workers=1)
    runner.register("p", handler)
    try:
        done = runner.wait(runner.submit("p", {}).id, timeout=10)
    finally:
        runner.shutdown()
    assert done.status == "succeeded"


def test_wait_returns_when_finish_keeps_failing(store):
    flaky = _flaky(store, finish=100)
    runner = JobRunner(flaky, max_workers=1, retry_delays=(0.0,))
    runner.register("ok", lambda p, c: 1)
    try:
        job = runner.submit("ok", {})
        started = time.monotonic()
        stuck = runner.wait(job.id)  # no timeout: must not poll forever
        assert time.monotonic() - started < 5
        assert stuck.status == "running"
        assert not runner.is_tracked(job.id)
    finally:
        runner.shutdown()


# ---- cooperative cancellation ----------------------------------------------


def _cancellable_loop(params: dict, ctx: JobContext) -> str:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        ctx.check_cancelled()
        time.sleep(0.01)
    return "ran to the end"


def test_running_cancellable_job_stops_at_next_checkpoint(store):
    runner = JobRunner(store, max_workers=1)
    runner.register("lab", _cancellable_loop, cancellable=True)
    try:
        job = runner.submit("lab", {})
        _wait_for_status(store, job.id, "running")
        requested = runner.cancel(job.id)
        # the handler may already have reached its checkpoint
        assert requested.status in ("running", "cancelled")
        done = runner.wait(job.id, timeout=5)
        assert done.status == "cancelled"
        assert done.result is None
    finally:
        runner.shutdown()


def test_running_non_cancellable_job_cancel_is_conflict(store):
    runner = JobRunner(store, max_workers=1)
    gate = threading.Event()
    runner.register("tick", lambda p, c: gate.wait(10), lock="tick")
    try:
        job = runner.submit("tick", {})
        _wait_for_status(store, job.id, "running")
        with pytest.raises(ConflictError):
            runner.cancel(job.id)
    finally:
        gate.set()
        runner.shutdown()


def test_runner_cancel_of_queued_job(store):
    runner = JobRunner(store, max_workers=1)
    gate = threading.Event()
    runner.register("block", lambda p, c: gate.wait(10))
    try:
        first = runner.submit("block", {})
        _wait_for_status(store, first.id, "running")
        second = runner.submit("block", {})
        assert runner.cancel(second.id).status == "cancelled"
    finally:
        gate.set()
        runner.shutdown()


def test_shutdown_requests_cancellation_of_running_cancellable_jobs(store):
    runner = JobRunner(store, max_workers=1)
    runner.register("lab", _cancellable_loop, cancellable=True)
    job = runner.submit("lab", {})
    _wait_for_status(store, job.id, "running")
    started = time.monotonic()
    runner.shutdown(wait=True)
    assert time.monotonic() - started < 5
    assert store.get(job.id).status == "cancelled"
