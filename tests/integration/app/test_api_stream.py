"""The live engine over the API (roadmap 21.3.4): the status route and the
stream and engine families on /metrics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from stonks.engine.monitor import LatencyHistogram
from stonks.engine.status import EngineStatusStore
from stonks.streaming.health import StreamHealth
from tests.integration.app.conftest import AUTH


def _write_engine(settings, *, now: datetime) -> None:
    health = StreamHealth(source="eodhd", state="streaming", bars_written=7, late_ticks=1)
    health.last_event_at = now - timedelta(seconds=3)
    lag = LatencyHistogram()
    lag.observe(0.3)
    order = LatencyHistogram()
    order.observe(0.8)
    EngineStatusStore(settings.state.path).write(
        {
            "engine_id": "intraday",
            "calendar": "24/7",
            "state": "streaming",
            "last_dispatch_at": (now - timedelta(seconds=20)).isoformat(),
            "driver": {"events": 50, "bars": 8, "bar_closes": 8, "handler_errors": {"step": 1}},
            "stream": health.snapshot(),
            "latency": {"dispatch_lag": lag.as_dict(), "event_to_order": order.as_dict()},
        },
        now=now,
    )


def test_status_with_no_engine(client):
    resp = client.get("/api/stream/status", headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["engines"] == []
    assert body["streaming_enabled"] is False
    assert body["deadman_minutes"] == 5
    assert body["intraday_pnl"]["available"] is False
    assert "21.3.3" in body["intraday_pnl"]["note"]


def test_status_reads_the_engine_row(client, settings):
    _write_engine(settings, now=datetime.now(UTC))
    body = client.get("/api/stream/status", headers=AUTH).json()
    [engine] = body["engines"]
    assert engine["engine_id"] == "intraday"
    assert engine["live"] is True
    assert engine["market_open"] is True  # 24/7
    assert engine["deadman"] == "ok"
    assert engine["bar_closes"] == 8
    assert engine["handler_errors"] == {"step": 1}
    assert engine["stream"]["connected"] is True
    assert engine["stream"]["bars_written"] == 7
    assert engine["dispatch_lag"]["count"] == 1
    assert engine["event_to_order"]["p50_seconds"] == 1.0


def test_status_needs_a_credential_remotely(remote):
    assert remote.get("/api/stream/status").status_code == 401


def test_status_declares_its_permission():
    from stonks.api.deps import route_permissions
    from stonks.api.routers import stream
    from stonks.auth import Permission

    perms = {(m, p): perm for m, p, perm in route_permissions(stream.router.routes)}
    assert perms[("GET", "/api/stream/status")] == Permission.READ


def test_metrics_carry_stream_and_engine_families(client, settings):
    _write_engine(settings, now=datetime.now(UTC))
    text = client.get("/metrics").text
    assert 'stonks_stream_up{engine="intraday",source="eodhd"} 1' in text
    assert "stonks_stream_last_event_age_seconds" in text
    assert 'stonks_stream_bars_written_total{engine="intraday",source="eodhd"} 7' in text
    assert 'stonks_stream_late_ticks_total{engine="intraday",source="eodhd"} 1' in text
    assert "stonks_engine_last_dispatch_age_seconds" in text
    assert 'stonks_engine_handler_errors_total{engine="intraday",handler="step"} 1' in text
    assert "# TYPE stonks_engine_dispatch_lag_seconds histogram" in text
    assert 'stonks_engine_event_to_order_seconds_count{engine="intraday"} 1' in text
    # each family once, even next to the scheduler's own
    assert text.count("# TYPE stonks_engine_up gauge") == 1


def test_metrics_without_an_engine_have_no_engine_families(client):
    assert "stonks_engine_up" not in client.get("/metrics").text
