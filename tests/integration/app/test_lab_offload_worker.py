"""Lab offload end to end (roadmap 14.9): the API queues lab jobs, an
in-process lab worker runs them on a read-only lake snapshot, and the
result comes back through the same job row."""

from __future__ import annotations

import threading
import time
from datetime import date

import pytest

from stonks.app.context import AppContext
from stonks.app.errors import ConflictError
from stonks.app.lab import LabRunRequest
from stonks.app.services import Services
from stonks.app.strategies import StrategyRef
from stonks.app.sweep import SweepRequest
from stonks.lab.offload.executor import WorkerLabExecutor
from stonks.lab.offload.settings import LabOffloadSettings
from stonks.lab.offload.snapshot import LakeSnapshots
from stonks.lab.offload.worker import SnapshotContext, build_worker
from stonks.lab.parallel import ParallelSettings


def _request(**overrides) -> LabRunRequest:
    base = {
        "strategy": StrategyRef(class_path="stonks.strategies.examples.momentum:Momentum"),
        "universe": ["UP.US", "DOWN.US"],
        "start": date(2025, 10, 1),
        "end": date(2026, 4, 1),
        "tuner": "random",
        "budget": 2,
        "survival_tests": ["oos"],
    }
    base.update(overrides)
    return LabRunRequest(**base)


@pytest.fixture
def offload_settings(settings):
    settings.lab = settings.lab.model_copy(
        update={
            "parallel": ParallelSettings(max_workers=1),
            "offload": LabOffloadSettings(
                executor="worker", heartbeat_seconds=0.05, poll_seconds=0.05
            ),
        }
    )
    return settings


@pytest.fixture
def api(offload_settings, seeded, fake_source):
    svc = Services.create(AppContext(offload_settings, source_factory=lambda: fake_source))
    svc.start()
    yield svc
    svc.shutdown()


@pytest.fixture
def worker(offload_settings, seeded):
    return build_worker(offload_settings, worker_id="w-test")


def test_the_api_queues_lab_runs_for_the_worker(api):
    assert isinstance(api.runner.lab_executor, WorkerLabExecutor)
    job = api.lab.submit_lab_run(_request())
    assert api.runner.store.executor_of(job.id) == "worker"
    assert api.jobs.wait(job.id, timeout=0.3).status == "queued"
    assert api.jobs.is_tracked(job.id)
    snapshot = api.runner.lab_executor.snapshots.current()
    assert snapshot is not None and snapshot.path.exists()


def test_a_run_that_fetches_data_first_stays_in_process(api):
    assert not api.runner.lab_executor.offloads("lab_run", {"ensure_data": True})


def test_the_worker_runs_a_lab_run_and_registers_the_result(api, worker):
    job = api.lab.submit_lab_run(_request(register_strategy=True))
    assert worker.run_once() == job.id
    done = api.jobs.wait(job.id, timeout=5)
    assert done.status == "succeeded", done.error
    assert [r["test_id"] for r in done.result["survival_reports"]] == ["oos"]
    sid = done.result["registered_strategy_id"]
    assert api.strategies.get(sid).status == "shadow"
    assert worker.run_once() is None  # the queue is empty
    stats = worker.queue.stats(lease_seconds=60)
    assert stats.outcomes["succeeded"] == 1
    assert stats.workers_alive == 1


def test_the_worker_runs_a_sweep(api, worker):
    job = api.lab.submit_sweep(
        SweepRequest(
            universe=["UP.US", "DOWN.US"],
            start=date(2025, 10, 1),
            end=date(2026, 4, 1),
            strategies=["momentum"],
            tuner="random",
            budget=1,
            survival_tests=["oos"],
        )
    )
    assert api.runner.store.executor_of(job.id) == "worker"
    assert worker.run_once() == job.id
    done = api.jobs.wait(job.id, timeout=5)
    assert done.status == "succeeded", done.error


def test_a_cancel_from_the_api_stops_the_worker_job(api, worker):
    job = api.lab.submit_lab_run(_request(budget=1000))
    stop = threading.Event()
    thread = threading.Thread(target=worker.run_forever, args=(stop,), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 30
        while api.jobs.get(job.id).status == "queued" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert api.jobs.cancel(job.id).message == "cancellation requested"
        done = api.jobs.wait(job.id, timeout=60)
        assert done.status == "cancelled"
    finally:
        stop.set()
        thread.join(timeout=30)
    assert worker.queue.stats(lease_seconds=60).workers_alive == 0  # stopped


def test_a_job_without_a_snapshot_fails_cleanly(offload_settings, seeded, worker):
    from stonks.app.jobs import JobStore

    store = JobStore(offload_settings.state.path)
    job = store.create("lab_run", _request().model_dump(mode="json"), executor="worker")
    assert worker.run_once() == job.id
    failed = store.get(job.id)
    assert failed.status == "failed"
    assert "no lake snapshot" in (failed.error or "")


def test_the_snapshot_context_refuses_without_a_snapshot(offload_settings, tmp_path):
    ctx = SnapshotContext(offload_settings, LakeSnapshots(tmp_path / "none"))
    with pytest.raises(ConflictError), ctx.lake():
        pass
    ctx.close()  # never closes the real lake


def test_a_stopping_worker_requeues_its_running_job(api, worker):
    """BE-42: a worker shut down mid-job hands the job back to the queue
    (a restart is not a user cancel)."""
    job = api.lab.submit_lab_run(_request(budget=1000))
    stop = threading.Event()
    thread = threading.Thread(target=worker.run_forever, args=(stop,), daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while api.jobs.get(job.id).status == "queued" and time.monotonic() < deadline:
        time.sleep(0.02)
    stop.set()
    worker.request_stop()
    thread.join(timeout=60)
    requeued = api.jobs.get(job.id)
    assert requeued.status == "queued"
    assert requeued.error is None
    assert worker.queue.claim_next("w2", ["lab_run"]) == job.id


def test_metrics_include_the_lab_queue(api):
    job = api.lab.submit_lab_run(_request())
    text = api.schedule.metrics()
    assert 'stonks_lab_queue_jobs{status="queued"} 1' in text
    assert job.id


def test_metrics_skip_the_lab_queue_when_the_state_db_fails(api, monkeypatch):
    def broken():
        raise OSError("disk gone")

    monkeypatch.setattr(api.context, "state", broken)
    assert api.schedule._lab_queue_metrics() == ""


# ---- python -m stonks.lab.offload ---------------------------------------------------


@pytest.fixture
def cli_env(settings, seeded, tmp_path, monkeypatch):
    monkeypatch.setenv("STONKS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STONKS_LAB_MAX_WORKERS", "1")
    monkeypatch.delenv("STONKS_LAB_EXECUTOR", raising=False)
    return ["--config", str(tmp_path / "none.toml")]


def test_cli_status_snapshot_and_one_worker_job(cli_env, settings, capsys, monkeypatch):
    import json

    from stonks.app.jobs import JobStore
    from stonks.lab.offload.__main__ import main

    assert main([*cli_env, "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["executor"] == "in_process"
    assert status["queued"] == 0
    assert main([*cli_env, "snapshot"]) == 0
    assert "snapshot" in json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    store = JobStore(settings.state.path)
    job = store.create("lab_run", _request().model_dump(mode="json"), executor="worker")
    tmp = settings.lake.path.parent / "lab_tmp"
    monkeypatch.setenv("TMPDIR", str(tmp))
    assert main([*cli_env, "worker", "--once", "--id", "w-cli"]) == 0
    assert tmp.is_dir()
    done = store.get(job.id)
    assert done.status == "succeeded", done.error


def test_cli_status_exits_one_when_jobs_wait_with_no_worker(cli_env, settings, capsys):
    from stonks.app.jobs import JobStore
    from stonks.lab.offload.__main__ import main
    from stonks.store.state import SqliteState

    job = JobStore(settings.state.path).create("lab_run", {}, executor="worker")
    with SqliteState(settings.state.path) as s:
        s.execute("UPDATE jobs SET created_at='2020-01-01T00:00:00+00:00' WHERE id=?", [job.id])
    assert main([*cli_env, "status"]) == 1
    assert "no live worker" in capsys.readouterr().out


def test_a_failed_heartbeat_is_logged_not_raised(worker):
    from stonks.app.jobs import JobContext
    from stonks.lab.offload.worker import _Heartbeat

    def touch() -> None:
        raise OSError("snapshot folder gone")

    ctx = JobContext(job_id="job_x", _store=worker.runner.store)
    _Heartbeat(worker, "job_x", ctx, touch).beat()  # no raise
    assert not ctx.cancel_requested


def test_the_worker_loop_survives_an_error(worker, monkeypatch):
    stop = threading.Event()
    calls: list[int] = []

    def flaky() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database is locked")
        stop.set()

    monkeypatch.setattr(worker, "run_once", flaky)
    worker.run_forever(stop)
    assert len(calls) == 2
