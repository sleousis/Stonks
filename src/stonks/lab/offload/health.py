"""Health and Prometheus metrics for the lab worker queue (roadmap 14.9).

The check fails when worker jobs wait with nobody to run them (queued
longer than the limit and no live worker) or a running job lost its
worker. It never trips the operational halt: a stuck lab queue does not
stop trading.
"""

from __future__ import annotations

from datetime import UTC, datetime

from stonks.lab.offload.queue import QueueStats, queue_stats
from stonks.lab.offload.settings import LabOffloadSettings
from stonks.scheduling.metrics import MetricFamily, Sample, render_prometheus
from stonks.store.state import SqliteState

#: The lease the health check and metrics assume for a live worker.
DEFAULT_LEASE_SECONDS = LabOffloadSettings().lease_seconds


def assess(stats: QueueStats, *, stuck_minutes: float) -> tuple[bool, str]:
    """``(ok, detail)`` for ``stats``."""
    summary = (
        f"{stats.queued} queued, {stats.running} running, {stats.workers_alive} worker(s) alive"
    )
    if stats.stale_running:
        return False, f"{stats.stale_running} running job(s) lost their worker; {summary}"
    waiting = stats.oldest_queued_seconds
    if waiting is not None and waiting > stuck_minutes * 60 and stats.workers_alive == 0:
        return False, f"oldest job waited {waiting / 60:.0f} min with no live worker; {summary}"
    return True, summary


def queue_health(
    state: SqliteState,
    *,
    stuck_minutes: float,
    now: datetime | None = None,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
) -> tuple[bool, str]:
    stats = queue_stats(state, lease_seconds=lease_seconds, now=now or datetime.now(UTC))
    return assess(stats, stuck_minutes=stuck_minutes)


def metric_families(stats: QueueStats) -> list[MetricFamily]:
    return [
        MetricFamily(
            "stonks_lab_queue_jobs",
            "Lab worker jobs by status (queued or running).",
            "gauge",
            [
                Sample(float(stats.queued), {"status": "queued"}),
                Sample(float(stats.running), {"status": "running"}),
            ],
        ),
        MetricFamily(
            "stonks_lab_queue_oldest_queued_seconds",
            "Age of the oldest queued lab worker job (0 when none waits).",
            "gauge",
            [Sample(stats.oldest_queued_seconds or 0.0)],
        ),
        MetricFamily(
            "stonks_lab_queue_stale_running",
            "Running lab worker jobs whose worker stopped sending heartbeats.",
            "gauge",
            [Sample(float(stats.stale_running))],
        ),
        MetricFamily(
            "stonks_lab_workers_alive",
            "Lab worker processes with a recent heartbeat.",
            "gauge",
            [Sample(float(stats.workers_alive))],
        ),
        MetricFamily(
            "stonks_lab_worker_jobs_total",
            "Jobs finished by lab workers, by outcome.",
            "counter",
            [Sample(float(n), {"outcome": k}) for k, n in sorted(stats.outcomes.items())],
        ),
    ]


def metrics_text(
    state: SqliteState,
    *,
    now: datetime | None = None,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
) -> str:
    stats = queue_stats(state, lease_seconds=lease_seconds, now=now or datetime.now(UTC))
    return render_prometheus(metric_families(stats))
