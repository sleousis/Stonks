"""The live engine status view (roadmap 21.3.4)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from stonks.app.stream import engine_view, latency_view
from stonks.engine.monitor import LatencyHistogram
from stonks.engine.status import EngineStatus
from stonks.scheduling.calendar import MarketCalendar, Session
from stonks.streaming.health import StreamHealth

OPEN = datetime(2026, 9, 28, 13, 30, tzinfo=UTC)
CLOSE = datetime(2026, 9, 28, 20, 0, tzinfo=UTC)


class OneDay(MarketCalendar):
    name = "one-day"

    def session(self, day: date) -> Session | None:
        return Session(day, OPEN, CLOSE) if day == OPEN.date() else None


def cal(name: str) -> MarketCalendar:
    return OneDay()


def status(*, updated=OPEN + timedelta(minutes=10), stopped=None, error=None) -> EngineStatus:
    health = StreamHealth(source="eodhd", state="streaming", bars_written=9, late_ticks=1)
    health.last_event_at = OPEN + timedelta(minutes=10)
    health.last_error = error
    lag = LatencyHistogram()
    lag.observe(0.2)
    return EngineStatus(
        engine_id="e1",
        calendar="XNYS",
        state="streaming",
        started_at=OPEN,
        updated_at=updated,
        stopped_at=stopped,
        last_dispatch_at=OPEN + timedelta(minutes=10),
        snapshot={
            "driver": {
                "bar_closes": 10,
                "bars": 20,
                "late_bars": 0,
                "pending_closes": 1,
                "handler_errors": {"monitor.dispatch": 1, "step": 2},
            },
            "stream": health.snapshot(),
            "latency": {"dispatch_lag": lag.as_dict()},
        },
    )


def view(s: EngineStatus, now: datetime):
    return engine_view(
        s, now, deadman_minutes=5, stale_after=timedelta(minutes=2), calendar_for=cal
    )


def test_a_healthy_engine():
    v = view(status(), OPEN + timedelta(minutes=11))
    assert v.live
    assert v.market_open
    assert v.deadman == "ok"
    assert v.bar_closes == 10
    assert v.handler_errors == {"step": 2}
    assert v.stream is not None
    assert v.stream.connected
    assert v.stream.last_event_age_seconds == 60
    assert v.dispatch_lag.count == 1
    assert v.dispatch_lag.p95_seconds == 0.25
    assert v.event_to_order.count == 0
    assert v.event_to_order.p50_seconds is None


def test_a_silent_engine_in_market_hours():
    v = view(status(updated=OPEN + timedelta(minutes=20)), OPEN + timedelta(minutes=20))
    assert v.deadman == "silent"
    assert v.silent_seconds == 600


def test_a_dead_process_is_not_live_and_its_stream_not_connected():
    v = view(status(), OPEN + timedelta(minutes=40))
    assert not v.live
    assert v.stream is not None
    assert not v.stream.connected


def test_closed_and_stopped():
    assert view(status(), CLOSE + timedelta(hours=1)).deadman == "closed"
    stopped = view(status(stopped=OPEN + timedelta(minutes=11)), OPEN + timedelta(minutes=30))
    assert stopped.deadman == "stopped"
    assert not stopped.live


def test_stream_errors_are_scrubbed():
    s = status(error="connect failed: wss://x/ws/us?api_token=SECRET123 refused")
    v = view(s, OPEN + timedelta(minutes=11))
    assert v.stream is not None
    assert "SECRET123" not in (v.stream.last_error or "")


def test_latency_view_mean():
    h = LatencyHistogram()
    for x in (0.1, 0.3):
        h.observe(x)
    v = latency_view(h)
    assert v.count == 2
    assert v.mean_seconds == 0.2
    assert v.max_seconds == 0.3
