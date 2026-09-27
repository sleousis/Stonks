"""The job runner with a lab executor that offloads (roadmap 14.9)."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any

import pytest

from stonks.app.errors import ConflictError
from stonks.app.jobs import JobCancelled, JobContext, JobRunner, JobStore
from stonks.lab.offload.executor import InProcessLabExecutor, LabExecutor
from stonks.lab.offload.queue import LabQueue
from stonks.store.state import SqliteState


class _FakeExecutor(LabExecutor):
    name = "worker"

    def __init__(self, queue: LabQueue) -> None:
        self.queue = queue
        self.prepared: list[str] = []
        self.cancels: list[str] = []

    def offloads(self, kind: str, params: Mapping[str, Any]) -> bool:
        return kind == "heavy"

    def prepare(self, kind: str, params: Mapping[str, Any]) -> None:
        self.prepared.append(kind)

    def tracks(self, job_id: str) -> bool:
        return self.queue.is_pending(job_id)

    def request_cancel(self, job_id: str) -> bool:
        self.cancels.append(job_id)
        return self.queue.request_cancel(job_id)


@pytest.fixture
def path(tmp_path):
    p = tmp_path / "state.sqlite"
    with SqliteState(p) as s:
        s.migrate()
    return p


@pytest.fixture
def executor(path):
    return _FakeExecutor(LabQueue(path))


@pytest.fixture
def runner(path, executor):
    r = JobRunner(JobStore(path), max_workers=1, lab_executor=executor)
    yield r
    r.shutdown()


def test_the_default_executor_runs_everything_in_process(path):
    runner = JobRunner(JobStore(path))
    try:
        assert isinstance(runner.lab_executor, InProcessLabExecutor)
        runner.register("heavy", lambda p, c: {"ran": True})
        job = runner.submit("heavy", {})
        assert runner.wait(job.id, timeout=10).result == {"ran": True}
        assert runner.store.executor_of(job.id) == "local"
    finally:
        runner.shutdown()


def test_an_offloaded_job_is_queued_for_the_worker_and_not_run_here(runner, executor):
    calls: list[Any] = []
    runner.register("heavy", lambda p, c: calls.append(p), cancellable=True)
    runner.register("light", lambda p, c: "local")
    job = runner.submit("heavy", {"x": 1})
    assert executor.prepared == ["heavy"]
    assert runner.store.executor_of(job.id) == "worker"
    assert runner.is_tracked(job.id)
    assert runner.wait(job.id, timeout=0.2).status == "queued"
    assert calls == []
    light = runner.submit("light", {})
    assert runner.wait(light.id, timeout=10).result == "local"


def test_wait_sees_the_worker_finish_the_job(runner, executor, path):
    runner.register("heavy", lambda p, c: None)
    job = runner.submit("heavy", {})
    claimed = executor.queue.claim_next("w1", ["heavy"])
    assert claimed == job.id

    def finish() -> None:
        JobStore(path).finish(job.id, "succeeded", result={"answer": 42})

    timer = threading.Timer(0.2, finish)
    timer.start()
    done = runner.wait(job.id)
    timer.join()
    assert done.status == "succeeded"
    assert done.result == {"answer": 42}
    assert not runner.is_tracked(job.id)


def test_a_queued_offloaded_job_can_be_cancelled(runner, executor):
    runner.register("heavy", lambda p, c: None)
    job = runner.submit("heavy", {})
    assert runner.cancel(job.id).status == "cancelled"
    assert executor.queue.claim_next("w1", ["heavy"]) is None


def test_a_running_offloaded_job_gets_a_cancel_request(runner, executor):
    runner.register("heavy", lambda p, c: None, cancellable=True)
    job = runner.submit("heavy", {})
    executor.queue.claim_next("w1", ["heavy"])
    runner.cancel(job.id)
    assert executor.cancels == [job.id]
    assert executor.queue.cancel_requested(job.id)


def test_a_running_offloaded_job_that_is_not_cancellable_refuses(runner, executor):
    runner.register("heavy", lambda p, c: None)
    job = runner.submit("heavy", {})
    executor.queue.claim_next("w1", ["heavy"])
    with pytest.raises(ConflictError):
        runner.cancel(job.id)


def test_restart_recovery_leaves_worker_jobs_alone(runner, path):
    runner.register("heavy", lambda p, c: None)
    job = runner.submit("heavy", {})
    store = JobStore(path)
    local = store.create("light", {})
    assert store.recover_interrupted() == 1
    assert store.get(job.id).status == "queued"
    assert store.get(local.id).status == "failed"


def test_run_claimed_executes_a_job_another_process_claimed(path):
    store = JobStore(path)
    queue = LabQueue(path)
    runner = JobRunner(store, max_workers=1)
    try:
        seen: list[dict[str, Any]] = []

        def handler(params: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
            seen.append(params)
            ctx.progress(0.5, "half")
            return {"double": params["x"] * 2}

        runner.register("heavy", handler)
        job = store.create("heavy", {"x": 21}, executor="worker")
        assert queue.claim_next("w1", ["heavy"]) == job.id
        outcome = runner.run_claimed(job.id)
        assert outcome == "succeeded"
        done = store.get(job.id)
        assert done.status == "succeeded"
        assert done.result == {"double": 42}
        assert seen == [{"x": 21}]
    finally:
        runner.shutdown()


def test_run_claimed_reports_failures_and_cancellation(path):
    store = JobStore(path)
    queue = LabQueue(path)
    runner = JobRunner(store, max_workers=1)
    try:

        def boom(params: dict[str, Any], ctx: JobContext) -> None:
            raise RuntimeError("kaput")

        def stop(params: dict[str, Any], ctx: JobContext) -> None:
            raise JobCancelled("stop")

        runner.register("boom", boom)
        runner.register("stop", stop, cancellable=True)
        failing = store.create("boom", {}, executor="worker")
        queue.claim_next("w1", ["boom"])
        assert runner.run_claimed(failing.id) == "failed"
        assert "kaput" in (store.get(failing.id).error or "")
        cancelled = store.create("stop", {}, executor="worker")
        queue.claim_next("w1", ["stop"])
        ctx = JobContext(job_id=cancelled.id, _store=store)
        assert runner.run_claimed(cancelled.id, ctx) == "cancelled"
        assert store.get(cancelled.id).status == "cancelled"
    finally:
        runner.shutdown()


def test_run_claimed_fails_an_unknown_kind(path):
    store = JobStore(path)
    queue = LabQueue(path)
    runner = JobRunner(store, max_workers=1)
    try:
        job = store.create("nobody", {}, executor="worker")
        queue.claim_next("w1", ["nobody"])
        assert runner.run_claimed(job.id) == "failed"
        assert "unknown job kind" in (store.get(job.id).error or "")
    finally:
        runner.shutdown()
