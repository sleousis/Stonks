"""Lab worker queue health and metrics (roadmap 14.9)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from stonks.app.jobs import JobStore
from stonks.config import HealthConfig
from stonks.lab.offload.health import assess, metrics_text, queue_health
from stonks.lab.offload.queue import LabQueue, QueueStats
from stonks.production.health import _lab_queue
from stonks.store.state import SqliteState


@pytest.fixture
def path(tmp_path):
    p = tmp_path / "state.sqlite"
    with SqliteState(p) as s:
        s.migrate()
    return p


def _stats(**kw) -> QueueStats:
    base = {"queued": 0, "running": 0, "oldest_queued_seconds": None, "workers_alive": 0}
    base.update(kw)
    return QueueStats(**base)


def test_an_idle_queue_is_healthy():
    ok, detail = assess(_stats(), stuck_minutes=30)
    assert ok
    assert detail == "0 queued, 0 running, 0 worker(s) alive"


def test_jobs_waiting_long_with_no_worker_are_unhealthy():
    ok, detail = assess(_stats(queued=2, oldest_queued_seconds=3600), stuck_minutes=30)
    assert not ok
    assert "no live worker" in detail


def test_a_busy_worker_makes_a_long_wait_fine():
    ok, _ = assess(
        _stats(queued=2, running=1, oldest_queued_seconds=3600, workers_alive=1), stuck_minutes=30
    )
    assert ok


def test_a_running_job_that_lost_its_worker_is_unhealthy():
    ok, detail = assess(_stats(running=1, stale_running=1), stuck_minutes=30)
    assert not ok
    assert "lost their worker" in detail


def test_queue_health_reads_the_state_db(path):
    JobStore(path).create("lab_run", {}, executor="worker")
    later = datetime.now(UTC) + timedelta(hours=2)
    with SqliteState(path) as s:
        ok, _ = queue_health(s, stuck_minutes=30, now=later)
    assert not ok


def test_the_production_health_check_wraps_it(path):
    config = HealthConfig()
    now = datetime.now(UTC)
    with SqliteState(path) as s:
        check = _lab_queue(s, config, now)
    assert check.name == "lab_queue"
    assert check.ok


def test_the_production_check_passes_without_the_table(tmp_path):
    with SqliteState(tmp_path / "bare.sqlite") as s:
        check = _lab_queue(s, HealthConfig(), datetime.now(UTC))
    assert check.ok
    assert check.detail == "no lab queue table"


def test_metrics_render_queue_and_worker_families(path):
    store = JobStore(path)
    store.create("lab_run", {}, executor="worker")
    queue = LabQueue(path)
    queue.register_worker("w1", host="h", pid=1, cpus=2)
    queue.worker_done("w1", "succeeded")
    with SqliteState(path) as s:
        text = metrics_text(s)
    assert 'stonks_lab_queue_jobs{status="queued"} 1' in text
    assert 'stonks_lab_queue_jobs{status="running"} 0' in text
    assert "stonks_lab_workers_alive 1" in text
    assert 'stonks_lab_worker_jobs_total{outcome="succeeded"} 1' in text
    assert "# TYPE stonks_lab_queue_oldest_queued_seconds gauge" in text
    assert "stonks_lab_queue_stale_running 0" in text
