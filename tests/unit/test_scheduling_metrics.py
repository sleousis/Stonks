"""Prometheus metrics and probes (roadmap 12.3)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from stonks.scheduling.jobs import JobSpec
from stonks.scheduling.metrics import (
    CONTENT_TYPE,
    MetricFamily,
    MetricsSnapshot,
    Sample,
    age_bucket,
    build_metrics,
    collect_snapshot,
    liveness,
    metrics_text,
    readiness,
    render_prometheus,
    scheduler_liveness,
)
from stonks.scheduling.runs import RunStore
from stonks.scheduling.triggers import SessionTrigger
from stonks.store.state import SqliteState

NOW = datetime(2026, 9, 25, 22, tzinfo=UTC)


# ---- rendering ----------------------------------------------------------------------


def test_render_text_format():
    text = render_prometheus(
        [
            MetricFamily(
                "stonks_x_total",
                "Help with \\ and\nnewline.",
                "counter",
                [Sample(3, {"b": "2", "a": 'q"uote'}), Sample(0.5)],
            )
        ]
    )
    assert text == (
        "# HELP stonks_x_total Help with \\\\ and\\nnewline.\n"
        "# TYPE stonks_x_total counter\n"
        'stonks_x_total{a="q\\"uote",b="2"} 3\n'
        "stonks_x_total 0.5\n"
    )
    assert CONTENT_TYPE.startswith("text/plain; version=0.0.4")


def test_age_buckets():
    today = date(2026, 9, 25)
    assert age_bucket(today, today) == "0-1d"
    assert age_bucket(date(2026, 9, 24), today) == "0-1d"
    assert age_bucket(date(2026, 9, 22), today) == "2-3d"
    assert age_bucket(date(2026, 9, 18), today) == "4-7d"
    assert age_bucket(date(2026, 9, 1), today) == "8d+"
    assert age_bucket(None, today) == "missing"


def _by_name(fams):
    return {f.name: f for f in fams}


def test_build_metrics_from_snapshot():
    snap = MetricsSnapshot(
        now=NOW,
        tick_counts={"ok": 5, "error": 1},
        last_tick_duration_seconds=12.5,
        last_tick_success=NOW - timedelta(hours=1),
        order_counts={"filled": 7, "rejected": 2},
        latest_bars={"A.US": date(2026, 9, 25), "B.US": date(2026, 9, 10), "C.US": None},
        job_counts={"queued": 2, "running": 1},
        scheduled={
            "tick": {"last_success": NOW, "last_status": "succeeded", "running": 0},
            "ingest": {"last_success": None, "last_status": "running", "running": 1},
        },
        next_runs={"tick": NOW + timedelta(days=1), "gone": None},
        scheduler_heartbeat=NOW,
    )
    fams = _by_name(build_metrics(snap))
    assert fams["stonks_tick_duration_seconds"].samples[0].value == 12.5
    assert fams["stonks_order_rejections_total"].samples[0].value == 2
    ages = {s.labels["bucket"]: s.value for s in fams["stonks_data_age_tickers"].samples}
    assert ages == {"0-1d": 1, "2-3d": 0, "4-7d": 0, "8d+": 1, "missing": 1}
    assert fams["stonks_data_oldest_bar_age_days"].samples[0].value == 15
    assert fams["stonks_job_queue_depth"].samples[0].value == 3
    assert [
        s.labels for s in fams["stonks_scheduled_job_last_success_timestamp_seconds"].samples
    ] == [{"job": "tick"}]
    assert len(fams["stonks_scheduled_job_next_run_timestamp_seconds"].samples) == 1
    text = render_prometheus(build_metrics(snap))
    assert 'stonks_tick_runs_total{status="ok"} 5' in text


def test_empty_snapshot_renders():
    text = render_prometheus(build_metrics(MetricsSnapshot(now=NOW)))
    assert "stonks_orders_total" in text
    assert "stonks_data_age_tickers" not in text


# ---- collection -------------------------------------------------------------------


def test_collect_from_state(tmp_path):
    path = tmp_path / "state.sqlite"
    with SqliteState(path) as s:
        s.migrate()
        s.execute(
            "INSERT INTO tick_runs (id, started_at, finished_at, status) VALUES "
            "('t1', '2026-09-24T21:00:00+00:00', '2026-09-24T21:00:30+00:00', 'ok'),"
            "('t2', '2026-09-25T21:00:00+00:00', '2026-09-25T21:00:05+00:00', 'error')"
        )
    store = RunStore(path)
    spec = JobSpec("tick", "tick", SessionTrigger("XNYS"))
    fire = spec.trigger.next_fire(datetime(2026, 9, 25, tzinfo=UTC))
    run_id = store.claim(spec, fire, instance_id="i", now=NOW, catch_up=False)
    store.finish(run_id, "succeeded", now=NOW)
    store.register_instance("i", host="h", pid=1, now=NOW)

    snap = collect_snapshot(path, now=NOW, specs=[spec])
    assert snap.tick_counts == {"ok": 1, "error": 1}
    assert snap.last_tick_duration_seconds == 5
    assert snap.last_tick_success == datetime(2026, 9, 24, 21, 0, 30, tzinfo=UTC)
    assert snap.scheduled["tick"]["last_status"] == "succeeded"
    assert snap.next_runs["tick"] == datetime(2026, 9, 28, 20, tzinfo=UTC)
    assert snap.scheduler_heartbeat == NOW
    assert "stonks_scheduler_heartbeat_timestamp_seconds" in metrics_text(path, now=NOW)


def test_broker_gateway_metrics(tmp_path):
    """Roadmap 19.4: labelled by gateway and mode, never by account."""
    path = tmp_path / "state.sqlite"
    with SqliteState(path) as s:
        s.migrate()
        s.execute(
            "INSERT INTO broker_gateway_status (gateway, mode, connected, last_check_at,"
            " last_ok_at) VALUES ('paper', 'paper', 1, '2026-09-25T21:55:00+00:00',"
            " '2026-09-25T21:55:00+00:00'), ('live', 'live', 0, '2026-09-25T21:55:00+00:00',"
            " NULL)"
        )
    snap = collect_snapshot(path, now=NOW)
    assert snap.brokers == [
        ("live", "live", False, None),
        ("paper", "paper", True, datetime(2026, 9, 25, 21, 55, tzinfo=UTC)),
    ]
    text = metrics_text(path, now=NOW)
    assert 'stonks_broker_connected{gateway="live",mode="live"} 0' in text
    assert 'stonks_broker_connected{gateway="paper",mode="paper"} 1' in text
    assert 'stonks_broker_last_ok_timestamp_seconds{gateway="paper",mode="paper"}' in text
    assert "stonks_broker_connected" not in render_prometheus(
        build_metrics(MetricsSnapshot(now=NOW))
    )


def test_collect_tolerates_unmigrated_db(tmp_path):
    snap = collect_snapshot(tmp_path / "empty.sqlite", now=NOW)
    assert snap.tick_counts == {} and snap.scheduled == {}


# ---- probes --------------------------------------------------------------------------


def test_liveness():
    assert liveness().ok


def test_readiness(tmp_path):
    path = tmp_path / "state.sqlite"
    assert not readiness(path).ok  # nothing migrated
    with SqliteState(path) as s:
        s.migrate()
    assert readiness(path).ok
    probe = readiness(path, tmp_path / "lake.duckdb")
    assert not probe.ok and "missing" in probe.checks["lake"]


def test_scheduler_liveness(tmp_path):
    store = RunStore(tmp_path / "state.sqlite")
    store.migrate()
    assert not scheduler_liveness(store, now=NOW).ok
    store.register_instance("i", host="h", pid=1, now=NOW)
    assert scheduler_liveness(store, now=NOW + timedelta(minutes=1)).ok
    assert not scheduler_liveness(store, now=NOW + timedelta(minutes=10)).ok
    store.mark_stopped("i", now=NOW)
    assert "stopped" in scheduler_liveness(store, now=NOW).checks["scheduler"]
