"""Health of a running stream (roadmap 21.1).

:class:`StreamHealth` is the runner's live record: state, connects and
disconnects, events per kind, bars written, late ticks, gaps and their
backfills, and the last error. :meth:`StreamHealth.snapshot` gives a JSON
view, and :func:`stream_metric_families` the Prometheus families, rendered
by :func:`stonks.scheduling.metrics.render_prometheus` like every other
metric. Labels carry the source id, never a key or an account.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from stonks.core.stream import StreamEvent
from stonks.scheduling.metrics import MetricFamily, Sample

StreamState = Literal["idle", "connecting", "streaming", "backoff", "stopped", "failed"]
STREAM_STATES: tuple[StreamState, ...] = (
    "idle",
    "connecting",
    "streaming",
    "backoff",
    "stopped",
    "failed",
)
GapReason = Literal["startup", "disconnect", "stale"]
_KINDS = ("trade", "quote", "bar", "heartbeat")


@dataclass
class Gap:
    """A stretch with no stream data. ``backfilled`` is ``None`` until a
    backfill ran (or when none was needed), then whether it worked."""

    start: datetime
    end: datetime | None
    reason: GapReason
    backfilled: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat() if self.end else None,
            "reason": self.reason,
            "backfilled": self.backfilled,
        }


@dataclass
class StreamHealth:
    source: str
    state: StreamState = "idle"
    started_at: datetime | None = None
    connected_at: datetime | None = None
    connects: int = 0
    disconnects: int = 0
    events: dict[str, int] = field(default_factory=lambda: dict.fromkeys(_KINDS, 0))
    #: The last trade, quote or bar (heartbeats do not count).
    last_event_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    bars_written: int = 0
    late_ticks: int = 0
    write_errors: int = 0
    subscriber_errors: int = 0
    backfills_ok: int = 0
    backfills_failed: int = 0
    gaps: deque[Gap] = field(default_factory=lambda: deque[Gap](maxlen=50))
    gaps_total: dict[str, int] = field(
        default_factory=lambda: {"startup": 0, "disconnect": 0, "stale": 0}
    )
    last_error: str | None = None

    def count(self, event: StreamEvent, now: datetime) -> None:
        self.events[event.kind] = self.events.get(event.kind, 0) + 1
        if event.kind == "heartbeat":
            self.last_heartbeat_at = now
        else:
            self.last_event_at = now

    def add_gap(self, gap: Gap) -> None:
        self.gaps.append(gap)
        self.gaps_total[gap.reason] = self.gaps_total.get(gap.reason, 0) + 1

    def snapshot(self) -> dict[str, Any]:
        def iso(when: datetime | None) -> str | None:
            return when.isoformat() if when else None

        return {
            "source": self.source,
            "state": self.state,
            "started_at": iso(self.started_at),
            "connected_at": iso(self.connected_at),
            "connects": self.connects,
            "disconnects": self.disconnects,
            "events": dict(self.events),
            "last_event_at": iso(self.last_event_at),
            "last_heartbeat_at": iso(self.last_heartbeat_at),
            "bars_written": self.bars_written,
            "late_ticks": self.late_ticks,
            "write_errors": self.write_errors,
            "subscriber_errors": self.subscriber_errors,
            "backfills": {"ok": self.backfills_ok, "failed": self.backfills_failed},
            "gaps": [g.as_dict() for g in self.gaps],
            "last_error": self.last_error,
        }


def _ts(when: datetime | None) -> float:
    return when.timestamp() if when else 0.0


def stream_metric_families(health: StreamHealth) -> list[MetricFamily]:
    src = {"source": health.source}

    def one(name: str, help_: str, kind: Literal["gauge", "counter"], value: float):
        return MetricFamily(name, help_, kind, [Sample(float(value), src)])

    return [
        one(
            "stonks_stream_up",
            "1 while the stream delivers.",
            "gauge",
            1 if health.state == "streaming" else 0,
        ),
        MetricFamily(
            "stonks_stream_state",
            "The stream runner's state (1 for the current one).",
            "gauge",
            [Sample(1.0 if health.state == s else 0.0, {**src, "state": s}) for s in STREAM_STATES],
        ),
        one("stonks_stream_connects_total", "Connections opened.", "counter", health.connects),
        one("stonks_stream_disconnects_total", "Connections lost.", "counter", health.disconnects),
        MetricFamily(
            "stonks_stream_events_total",
            "Events received, by kind.",
            "counter",
            [Sample(float(n), {**src, "kind": k}) for k, n in sorted(health.events.items())],
        ),
        one(
            "stonks_stream_last_event_timestamp_seconds",
            "When the last trade, quote or bar arrived (0 for never).",
            "gauge",
            _ts(health.last_event_at),
        ),
        one(
            "stonks_stream_bars_written_total",
            "Bars written to the bar store.",
            "counter",
            health.bars_written,
        ),
        one(
            "stonks_stream_late_ticks_total",
            "Ticks dropped for a closed bar.",
            "counter",
            health.late_ticks,
        ),
        one(
            "stonks_stream_write_errors_total",
            "Failed bar writes (kept for retry).",
            "counter",
            health.write_errors,
        ),
        MetricFamily(
            "stonks_stream_gaps_total",
            "Gaps in the stream, by reason.",
            "counter",
            [Sample(float(n), {**src, "reason": r}) for r, n in sorted(health.gaps_total.items())],
        ),
        MetricFamily(
            "stonks_stream_backfills_total",
            "REST backfills of gaps, by result.",
            "counter",
            [
                Sample(float(health.backfills_ok), {**src, "result": "ok"}),
                Sample(float(health.backfills_failed), {**src, "result": "failed"}),
            ],
        ),
    ]
