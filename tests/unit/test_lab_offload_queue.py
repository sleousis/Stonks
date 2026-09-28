"""The lab worker queue over the ``jobs`` table (roadmap 14.9)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from stonks.app.jobs import JobStore
from stonks.lab.offload.queue import LabQueue
from stonks.store.state import SqliteState


class _Clock:
    def __init__(self) -> None:
        # Starts at the real time: job rows carry real created_at stamps.
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def path(tmp_path):
    p = tmp_path / "state.sqlite"
    with SqliteState(p) as s:
        s.migrate()
    return p


@pytest.fixture
def clock():
    return _Clock()


@pytest.fixture
def queue(path, clock):
    return LabQueue(path, clock=clock)


def test_worker_jobs_are_created_with_the_worker_executor(path):
    store = JobStore(path)
    local = store.create("lab_run", {})
    remote = store.create("lab_run", {}, executor="worker")
    assert store.executor_of(local.id) == "local"
    assert store.executor_of(remote.id) == "worker"


def test_claim_takes_the_oldest_queued_worker_job_of_a_known_kind(path, queue):
    store = JobStore(path)
    store.create("lab_run", {"n": 0})  # local: never claimed by a worker
    other = store.create("mystery", {}, executor="worker")
    first = store.create("lab_run", {"n": 1}, executor="worker")
    second = store.create("lab_sweep", {"n": 2}, executor="worker")
    assert queue.claim_next("w1", ["lab_run", "lab_sweep"]) == first.id
    assert queue.claim_next("w2", ["lab_run", "lab_sweep"]) == second.id
    assert queue.claim_next("w1", ["lab_run", "lab_sweep"]) is None
    job = store.get(first.id)
    assert job.status == "running"
    assert job.started_at is not None
    assert store.get(other.id).status == "queued"


def test_claim_with_no_kinds_takes_nothing(path, queue):
    JobStore(path).create("lab_run", {}, executor="worker")
    assert queue.claim_next("w1", []) is None


def test_cancelled_queued_jobs_are_never_claimed(path, queue):
    store = JobStore(path)
    job = store.create("lab_run", {}, executor="worker")
    store.cancel(job.id)
    assert queue.claim_next("w1", ["lab_run"]) is None


def test_cancel_request_flags_only_a_running_worker_job(path, queue):
    store = JobStore(path)
    job = store.create("lab_run", {}, executor="worker")
    assert queue.request_cancel(job.id) is False  # still queued
    queue.claim_next("w1", ["lab_run"])
    assert queue.cancel_requested(job.id) is False
    assert queue.request_cancel(job.id) is True
    assert queue.cancel_requested(job.id) is True


def test_is_pending_is_true_for_non_terminal_worker_jobs_only(path, queue):
    store = JobStore(path)
    local = store.create("lab_run", {})
    remote = store.create("lab_run", {}, executor="worker")
    assert queue.is_pending(remote.id)
    assert not queue.is_pending(local.id)
    assert not queue.is_pending("job_missing")
    store.finish(remote.id, "succeeded", result={"ok": True})
    assert not queue.is_pending(remote.id)


def test_stale_running_jobs_are_reaped_after_the_lease(path, queue, clock):
    store = JobStore(path)
    job = store.create("lab_run", {}, executor="worker")
    queue.claim_next("w1", ["lab_run"])
    clock.advance(60)
    queue.heartbeat(job.id, "w1")
    clock.advance(100)
    assert queue.reap_stale(lease_seconds=120) == []
    clock.advance(30)
    assert queue.reap_stale(lease_seconds=120) == [job.id]
    reaped = store.get(job.id)
    assert reaped.status == "failed"
    assert "worker lost" in (reaped.error or "")


def test_heartbeat_of_another_worker_does_not_extend_the_lease(path, queue, clock):
    store = JobStore(path)
    job = store.create("lab_run", {}, executor="worker")
    queue.claim_next("w1", ["lab_run"])
    clock.advance(130)
    queue.heartbeat(job.id, "w2")
    assert queue.reap_stale(lease_seconds=120) == [job.id]


def test_stats_count_the_queue_and_live_workers(path, queue, clock):
    store = JobStore(path)
    store.create("lab_run", {}, executor="worker")
    store_job = store.create("lab_run", {}, executor="worker")
    clock.advance(30)
    queue.register_worker("w1", host="h", pid=1, cpus=4)
    queue.register_worker("w2", host="h", pid=2, cpus=4)
    queue.claim_next("w1", ["lab_run"])
    queue.worker_done("w1", "succeeded")
    queue.worker_done("w1", "failed")
    queue.stop_worker("w2")
    clock.advance(90)
    stats = queue.stats(lease_seconds=120)
    assert stats.queued == 1
    assert stats.running == 1
    # the first job was claimed, the second was created at about t0
    assert stats.oldest_queued_seconds == pytest.approx(120.0, abs=5.0)
    assert stats.workers_alive == 1
    assert stats.outcomes == {"succeeded": 1, "failed": 1, "cancelled": 0}
    assert store_job.id  # created after the first
    clock.advance(200)
    assert queue.stats(lease_seconds=120).workers_alive == 0


def test_stats_on_an_empty_queue(queue):
    stats = queue.stats(lease_seconds=120)
    assert stats.queued == 0
    assert stats.running == 0
    assert stats.oldest_queued_seconds is None
    assert stats.workers_alive == 0


def test_worker_beat_records_the_current_job(path, queue, clock):
    queue.register_worker("w1", host="h", pid=1, cpus=2)
    clock.advance(5)
    queue.beat_worker("w1", current_job_id="job_x")
    with SqliteState(path) as s:
        row = s.sql("SELECT * FROM lab_workers WHERE id='w1'")[0]
    assert row["current_job_id"] == "job_x"
    assert row["heartbeat_at"] == clock.now.isoformat(timespec="microseconds")


def test_registering_the_same_worker_id_twice_works(path, queue, clock):
    """BE-42: a restart with the same ``--id`` must not crash-loop."""
    queue.register_worker("w1", host="h", pid=1, cpus=2)
    queue.stop_worker("w1")
    clock.advance(5)
    queue.register_worker("w1", host="h", pid=2, cpus=4)
    with SqliteState(path) as s:
        rows = s.sql("SELECT pid, cpus, stopped_at FROM lab_workers WHERE id = 'w1'")
    assert [(r["pid"], r["cpus"], r["stopped_at"]) for r in rows] == [(2, 4, None)]


def test_requeue_hands_a_running_job_back(path, queue):
    store = JobStore(path)
    job = store.create("lab_run", {}, executor="worker")
    assert queue.claim_next("w1", ["lab_run"]) == job.id
    store.finish(job.id, "cancelled")
    assert queue.requeue(job.id, "w1")
    back = store.get(job.id)
    assert back.status == "queued" and back.started_at is None
    assert not queue.requeue(job.id, "w1")  # only a job that worker finished


def test_a_portable_claim_skips_jobs_that_name_a_registered_strategy(path, queue):
    """A remote worker has no server artifacts: it takes catalog classes
    and sweeps, and leaves registered refs to a worker on the server."""
    store = JobStore(path)
    registered = store.create(
        "lab_run", {"strategy": {"strategy_id": "bah_1", "params": {}}}, executor="worker"
    )
    catalog = store.create(
        "lab_run", {"strategy": {"class_path": "m:C", "strategy_id": None}}, executor="worker"
    )
    sweep = store.create("lab_sweep", {"strategies": ["momentum"]}, executor="worker")
    kinds = ["lab_run", "lab_sweep"]
    assert queue.claim_next("remote", kinds, portable_only=True) == catalog.id
    assert queue.claim_next("remote", kinds, portable_only=True) == sweep.id
    assert queue.claim_next("remote", kinds, portable_only=True) is None
    assert queue.claim_next("local", kinds) == registered.id


def test_release_puts_a_running_job_back_unless_a_cancel_was_asked(path, queue):
    store = JobStore(path)
    job = store.create("lab_run", {}, executor="worker")
    assert queue.claim_next("w1", ["lab_run"]) == job.id
    assert queue.is_running_on(job.id, "w1")
    assert not queue.is_running_on(job.id, "w2")
    assert not queue.release(job.id, "w2")  # not its job
    assert queue.release(job.id, "w1")
    requeued = store.get(job.id)
    assert (requeued.status, requeued.started_at) == ("queued", None)
    assert not queue.is_running_on(job.id, "w1")
    assert queue.claim_next("w2", ["lab_run"]) == job.id
    assert queue.request_cancel(job.id)
    assert not queue.release(job.id, "w2")  # a user cancel is not undone
    assert store.get(job.id).status == "running"
