"""Engine monitoring (roadmap 21.3.4).

:class:`EngineMonitor` watches one :class:`~stonks.engine.driver.EventDriver`
and, when it has one, the stream's
:class:`~stonks.streaming.health.StreamHealth`.

- It registers two driver handlers: ``monitor.dispatch`` runs first on each
  bar close and records the dispatch lag (clock time past the close plus
  settle), and ``monitor.publish`` runs last and writes the status row.
- :meth:`EngineMonitor.record_order` measures event to order latency: from
  the moment the bar close was dispatched to the moment an order went out.
  The decision step and the router call it.
- The status row (``engine_status``, see :mod:`stonks.engine.status`) is
  how the API, the metrics endpoint and the scheduler's dead-man read an
  engine that runs in another process. Publishing is throttled by wall
  time, and a failed write is logged, never raised into the engine.

Latency uses :class:`LatencyHistogram`, fixed buckets that render as a
Prometheus histogram.
"""

from __future__ import annotations

import bisect
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from stonks.logging import get_logger
from stonks.scheduling.metrics import MetricFamily, histogram_family

if TYPE_CHECKING:
    from stonks.engine.driver import BarClose, EventDriver
    from stonks.engine.status import EngineStatusStore
    from stonks.streaming.health import StreamHealth

_log = get_logger("stonks.engine.monitor")

#: Upper bounds in seconds. Minute decisions care about the band from tens
#: of milliseconds to a minute.
LATENCY_BOUNDS: tuple[float, ...] = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)

#: Dispatch times kept for :meth:`EngineMonitor.record_order`.
_KEEP_DISPATCHES = 64

DISPATCH_HANDLER = "monitor.dispatch"
PUBLISH_HANDLER = "monitor.publish"


@dataclass
class LatencyHistogram:
    """Readings in seconds, counted per bucket. ``counts`` has one entry per
    bound plus the ``+Inf`` bucket last."""

    bounds: tuple[float, ...] = LATENCY_BOUNDS
    counts: list[int] = field(default_factory=list)
    total: float = 0.0
    max: float = 0.0

    def __post_init__(self) -> None:
        if len(self.counts) != len(self.bounds) + 1:
            self.counts = [0] * (len(self.bounds) + 1)

    @property
    def count(self) -> int:
        return sum(self.counts)

    def observe(self, seconds: float) -> None:
        value = max(float(seconds), 0.0)  # clock skew never makes a negative reading
        self.counts[bisect.bisect_left(self.bounds, value)] += 1
        self.total += value
        self.max = max(self.max, value)

    def quantile(self, q: float) -> float | None:
        """An upper estimate: the bound of the bucket that holds the
        ``q`` quantile, or the largest reading for the ``+Inf`` bucket."""
        n = self.count
        if n == 0:
            return None
        target = q * n
        running = 0
        for i, c in enumerate(self.counts):
            running += c
            if running >= target and c:
                return self.bounds[i] if i < len(self.bounds) else self.max
        return self.max

    def as_dict(self) -> dict[str, Any]:
        return {
            "bounds": list(self.bounds),
            "counts": list(self.counts),
            "total": self.total,
            "max": self.max,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> LatencyHistogram:
        """Back from :meth:`as_dict`. A missing or malformed dict is empty."""
        if not data:
            return cls()
        try:
            bounds = tuple(float(b) for b in data["bounds"])
            counts = [int(c) for c in data["counts"]]
            if len(counts) != len(bounds) + 1:
                return cls()
            return cls(bounds, counts, float(data["total"]), float(data["max"]))
        except (KeyError, TypeError, ValueError):
            return cls()

    def family(self, name: str, help_: str, labels: Mapping[str, str]) -> MetricFamily:
        return histogram_family(
            name, help_, bounds=self.bounds, counts=self.counts, total=self.total, labels=labels
        )


def _iso(when: datetime | None) -> str | None:
    return when.isoformat() if when else None


class EngineMonitor:
    """Watches one driver. Call :meth:`attach` once, then :meth:`start`
    and :meth:`stop` around the run when a status store is set."""

    def __init__(
        self,
        driver: EventDriver,
        *,
        engine_id: str = "default",
        calendar: str = "XNYS",
        health: StreamHealth | None = None,
        store: EngineStatusStore | None = None,
        publish_seconds: float = 15.0,
        wall: Callable[[], float] = time.monotonic,
    ) -> None:
        self.driver = driver
        self.engine_id = engine_id
        self.calendar = calendar
        self.health = health
        self.store = store
        self.publish_seconds = publish_seconds
        self._wall = wall
        self._last_publish: float | None = None
        self.started_at: datetime | None = None
        self.last_dispatch_at: datetime | None = None
        self.dispatch_lag = LatencyHistogram()
        self.event_to_order = LatencyHistogram()
        self._dispatched: OrderedDict[int, datetime] = OrderedDict()

    # ---- wiring ----------------------------------------------------------------------

    def attach(self) -> None:
        """Register the two handlers: first and last on every bar close."""
        self.driver.register(self.on_dispatch, name=DISPATCH_HANDLER, priority=-(10**9))
        self.driver.register(self._after_dispatch, name=PUBLISH_HANDLER, priority=10**9)

    def start(self) -> None:
        now = self.driver.clock.now()
        self.started_at = now
        store = self.store
        if store is not None:
            self._safely(lambda: store.start(self.engine_id, calendar=self.calendar, now=now))

    def stop(self) -> None:
        """Publish the final counters and mark the row stopped."""
        if self.store is None:
            return
        self.publish(state="stopped")
        now = self.driver.clock.now()
        store = self.store
        self._safely(lambda: store.mark_stopped(self.engine_id, now=now))

    # ---- measurements ----------------------------------------------------------------

    def on_dispatch(self, event: BarClose) -> None:
        now = self.driver.clock.now()
        decide_at = event.at + self.driver.settle
        self.dispatch_lag.observe((now - decide_at).total_seconds())
        self.last_dispatch_at = now
        self._dispatched[event.sequence] = now
        while len(self._dispatched) > _KEEP_DISPATCHES:
            self._dispatched.popitem(last=False)

    def record_order(self, event: BarClose, submitted_at: datetime | None = None) -> float:
        """Event to order latency in seconds, from the dispatch of ``event``
        (or its settle time when that dispatch is not known) to
        ``submitted_at`` (default: now on the driver's clock)."""
        at = submitted_at or self.driver.clock.now()
        start = self._dispatched.get(event.sequence, event.at + self.driver.settle)
        seconds = max((at - start).total_seconds(), 0.0)
        self.event_to_order.observe(seconds)
        return seconds

    # ---- status ----------------------------------------------------------------------

    def snapshot(self, *, state: str | None = None) -> dict[str, Any]:
        stats = self.driver.stats
        errors = {k: v for k, v in stats.handler_errors.items() if not k.startswith("monitor.")}
        stream = self.health.snapshot() if self.health is not None else None
        return {
            "engine_id": self.engine_id,
            "calendar": self.calendar,
            "state": state or (self.health.state if self.health is not None else "running"),
            "started_at": _iso(self.started_at),
            "last_dispatch_at": _iso(self.last_dispatch_at),
            "driver": {
                "events": stats.events,
                "bars": stats.bars,
                "bar_closes": stats.bar_closes,
                "late_bars": stats.late_bars,
                "handler_errors": errors,
                "pending_closes": self.driver.pending_closes,
                "last_close_at": _iso(stats.last_close_at),
                "interval": self.driver.interval.code,
                "settle_seconds": self.driver.settle / timedelta(seconds=1),
            },
            "stream": stream,
            "latency": {
                "dispatch_lag": self.dispatch_lag.as_dict(),
                "event_to_order": self.event_to_order.as_dict(),
            },
        }

    def publish(self, *, state: str | None = None) -> None:
        """Write the status row now (no throttle). Never raises."""
        if self.store is None:
            return
        store = self.store
        snap = self.snapshot(state=state)
        now = self.driver.clock.now()
        self._safely(lambda: store.write(snap, now=now))

    def _after_dispatch(self, event: BarClose) -> None:
        if self.store is None:
            return
        wall = self._wall()
        if self._last_publish is not None and wall - self._last_publish < self.publish_seconds:
            return
        self._last_publish = wall
        self.publish()

    def _safely(self, write: Callable[[], object]) -> None:
        try:
            write()
        except Exception as exc:  # monitoring must never stop trading
            _log.warning(
                "engine.status_write_failed",
                engine=self.engine_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
