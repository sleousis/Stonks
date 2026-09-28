"""The engine status row and its Prometheus families (roadmap 21.3.4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from stonks.engine.monitor import LatencyHistogram
from stonks.engine.status import (
    EngineStatusStore,
    engine_metric_families,
    engine_metrics_text,
)
from stonks.scheduling.metrics import render_prometheus
from stonks.store.state import SqliteState
from stonks.streaming.health import StreamHealth

NOW = datetime(2026, 9, 28, 15, 0, tzinfo=UTC)


@pytest.fixture
def state_path(tmp_path):
    path = tmp_path / "state.sqlite"
    with SqliteState(path) as s:
        s.migrate()
    return path


def snapshot(engine_id="e1", *, state="streaming", last_event=NOW, lag=(0.2,), orders=(0.4,)):
    health = StreamHealth(source="eodhd", state=state, bars_written=12, late_ticks=2)
    health.last_event_at = last_event
    lag_h, order_h = LatencyHistogram(), LatencyHistogram()
    for v in lag:
        lag_h.observe(v)
    for v in orders:
        order_h.observe(v)
    return {
        "engine_id": engine_id,
        "calendar": "XNYS",
        "state": state,
        "last_dispatch_at": (NOW - timedelta(seconds=30)).isoformat(),
        "driver": {
            "events": 100,
            "bars": 20,
            "bar_closes": 10,
            "late_bars": 1,
            "handler_errors": {"step": 3},
            "pending_closes": 0,
        },
        "stream": health.snapshot(),
        "latency": {"dispatch_lag": lag_h.as_dict(), "event_to_order": order_h.as_dict()},
    }


def test_store_start_write_read_and_stop(state_path):
    store = EngineStatusStore(state_path)
    store.start("e1", calendar="XNYS", now=NOW - timedelta(hours=1))
    [row] = store.read_all()
    assert row.state == "starting"
    assert row.last_dispatch_at is None
    store.write(snapshot(), now=NOW)
    [row] = store.read_all()
    assert row.state == "streaming"
    assert row.started_at == NOW - timedelta(hours=1)
    assert row.updated_at == NOW
    assert row.last_dispatch_at == NOW - timedelta(seconds=30)
    assert row.stream is not None and row.stream.bars_written == 12
    assert row.latency("event_to_order").count == 1
    store.mark_stopped("e1", now=NOW + timedelta(minutes=1))
    [row] = store.read_all()
    assert row.stopped_at == NOW + timedelta(minutes=1)
    assert row.state == "stopped"


def test_a_restart_clears_the_stop(state_path):
    store = EngineStatusStore(state_path)
    store.start("e1", calendar="XNYS", now=NOW)
    store.mark_stopped("e1", now=NOW)
    store.start("e1", calendar="XNYS", now=NOW + timedelta(hours=1))
    [row] = store.read_all()
    assert row.stopped_at is None
    assert row.started_at == NOW + timedelta(hours=1)


def test_write_without_start_creates_the_row(state_path):
    store = EngineStatusStore(state_path)
    store.write(snapshot("e2"), now=NOW)
    [row] = store.read_all()
    assert row.engine_id == "e2"
    assert row.started_at == NOW


def test_read_all_on_an_old_db_is_empty(tmp_path):
    assert EngineStatusStore(tmp_path / "empty.sqlite").read_all() == []


def test_is_live_needs_a_recent_update(state_path):
    store = EngineStatusStore(state_path)
    store.write(snapshot(), now=NOW)
    [row] = store.read_all()
    assert row.is_live(NOW + timedelta(seconds=60), stale_after=timedelta(minutes=2))
    assert not row.is_live(NOW + timedelta(minutes=5), stale_after=timedelta(minutes=2))


def test_families_cover_stream_and_engine(state_path):
    store = EngineStatusStore(state_path)
    store.write(snapshot("e1"), now=NOW)
    store.write(snapshot("e2", last_event=NOW - timedelta(seconds=90)), now=NOW)
    text = render_prometheus(
        engine_metric_families(
            store.read_all(), now=NOW + timedelta(seconds=10), stale_after=timedelta(minutes=2)
        )
    )
    assert text.count("# TYPE stonks_stream_up gauge") == 1
    assert 'stonks_stream_up{engine="e1",source="eodhd"} 1' in text
    assert 'stonks_stream_last_event_age_seconds{engine="e2",source="eodhd"} 100' in text
    assert 'stonks_stream_bars_written_total{engine="e1",source="eodhd"} 12' in text
    assert 'stonks_stream_late_ticks_total{engine="e1",source="eodhd"} 2' in text
    assert 'stonks_engine_up{engine="e1"} 1' in text
    assert 'stonks_engine_bar_closes_total{engine="e1"} 10' in text
    assert 'stonks_engine_bars_total{engine="e1"} 20' in text
    assert 'stonks_engine_late_bars_total{engine="e1"} 1' in text
    assert 'stonks_engine_handler_errors_total{engine="e1",handler="step"} 3' in text
    assert 'stonks_engine_last_dispatch_age_seconds{engine="e1"} 40' in text
    assert "# TYPE stonks_engine_dispatch_lag_seconds histogram" in text
    assert 'stonks_engine_dispatch_lag_seconds_count{engine="e1"} 1' in text
    assert "# TYPE stonks_engine_event_to_order_seconds histogram" in text
    assert 'stonks_engine_event_to_order_seconds_bucket{engine="e2",le="0.5"} 1' in text


def test_a_stale_engine_reports_down_and_its_stream_down(state_path):
    store = EngineStatusStore(state_path)
    store.write(snapshot("e1"), now=NOW)
    text = render_prometheus(
        engine_metric_families(
            store.read_all(), now=NOW + timedelta(hours=1), stale_after=timedelta(minutes=2)
        )
    )
    assert 'stonks_engine_up{engine="e1"} 0' in text
    assert 'stonks_stream_up{engine="e1",source="eodhd"} 0' in text


def test_metrics_text_is_empty_without_engines(state_path, tmp_path):
    assert engine_metrics_text(state_path, now=NOW) == ""
    assert engine_metrics_text(tmp_path / "missing.sqlite", now=NOW) == ""


def test_metrics_text_renders_an_engine(state_path):
    EngineStatusStore(state_path).write(snapshot(), now=NOW)
    assert "stonks_engine_up" in engine_metrics_text(state_path, now=NOW)
