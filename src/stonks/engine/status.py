"""The live engine's status row and its metrics (roadmap 21.3.4).

The engine runs in its own process. Its
:class:`~stonks.engine.monitor.EngineMonitor` writes one ``engine_status``
row (SQLite migration 044) through :class:`EngineStatusStore`, and other
processes read it:

- ``GET /metrics`` renders :func:`engine_metric_families` (stream health,
  driver counters, dispatch lag and event to order histograms), labelled
  by engine id and source, never by key or account;
- ``GET /api/stream/status`` shows it in the console's live panel;
- the scheduler's watchdog runs the engine dead-man
  (:mod:`stonks.engine.deadman`) over it.

An engine whose row has not been updated within ``stale_after`` is not
live: its process died or hung, so its stream reports down too.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from stonks.engine.monitor import LatencyHistogram
from stonks.scheduling.metrics import MetricFamily, Sample, merge_families, render_prometheus
from stonks.store.state import SqliteState
from stonks.streaming.health import StreamHealth, stream_metric_families

TABLE = "engine_status"

#: A row not updated for this long belongs to an engine that is not live.
DEFAULT_STALE_AFTER = timedelta(minutes=2)

LatencyKind = Literal["dispatch_lag", "event_to_order"]


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).isoformat()


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True)
class EngineStatus:
    engine_id: str
    calendar: str
    state: str
    started_at: datetime
    updated_at: datetime
    stopped_at: datetime | None
    last_dispatch_at: datetime | None
    snapshot: Mapping[str, Any] = field(default_factory=dict)

    @property
    def stream(self) -> StreamHealth | None:
        data = self.snapshot.get("stream")
        return StreamHealth.from_snapshot(data) if isinstance(data, Mapping) else None

    @property
    def driver(self) -> Mapping[str, Any]:
        data = self.snapshot.get("driver")
        return data if isinstance(data, Mapping) else {}

    def latency(self, kind: LatencyKind) -> LatencyHistogram:
        data = self.snapshot.get("latency")
        return LatencyHistogram.from_dict(data.get(kind) if isinstance(data, Mapping) else None)

    def handler_errors(self) -> dict[str, int]:
        raw = self.driver.get("handler_errors")
        if not isinstance(raw, Mapping):
            return {}
        return {str(k): int(v) for k, v in raw.items()}

    def is_live(self, now: datetime, *, stale_after: timedelta = DEFAULT_STALE_AFTER) -> bool:
        """Running, and its row was updated within ``stale_after``."""
        return self.stopped_at is None and now - self.updated_at <= stale_after


class EngineStatusStore:
    """Reads and writes ``engine_status``. Opens a connection per call, so
    the engine, the API and the scheduler can share it."""

    def __init__(self, state_path: str | Path) -> None:
        self.state_path = Path(state_path)

    def start(self, engine_id: str, *, calendar: str, now: datetime) -> None:
        """A fresh run: clears the stop and the last dispatch."""
        with SqliteState(self.state_path) as s:
            s.execute(
                f"INSERT INTO {TABLE} (engine_id, calendar, state, started_at, updated_at,"
                " stopped_at, last_dispatch_at, snapshot_json)"
                " VALUES (?, ?, 'starting', ?, ?, NULL, NULL, '{}')"
                " ON CONFLICT (engine_id) DO UPDATE SET calendar = excluded.calendar,"
                " state = 'starting', started_at = excluded.started_at,"
                " updated_at = excluded.updated_at, stopped_at = NULL,"
                " last_dispatch_at = NULL, snapshot_json = '{}'",
                [engine_id, calendar, _iso(now), _iso(now)],
            )

    def write(self, snapshot: Mapping[str, Any], *, now: datetime) -> None:
        """Store a :meth:`EngineMonitor.snapshot`. Creates the row when
        :meth:`start` was not called."""
        engine_id = str(snapshot["engine_id"])
        last = snapshot.get("last_dispatch_at")
        with SqliteState(self.state_path) as s:
            s.execute(
                f"INSERT INTO {TABLE} (engine_id, calendar, state, started_at, updated_at,"
                " stopped_at, last_dispatch_at, snapshot_json)"
                " VALUES (?, ?, ?, ?, ?, NULL, ?, ?)"
                " ON CONFLICT (engine_id) DO UPDATE SET calendar = excluded.calendar,"
                " state = excluded.state, updated_at = excluded.updated_at,"
                " last_dispatch_at = excluded.last_dispatch_at,"
                " snapshot_json = excluded.snapshot_json",
                [
                    engine_id,
                    str(snapshot.get("calendar") or "XNYS"),
                    str(snapshot.get("state") or "running"),
                    _iso(now),
                    _iso(now),
                    last if isinstance(last, str) else None,
                    json.dumps(snapshot, default=str),
                ],
            )

    def mark_stopped(self, engine_id: str, *, now: datetime) -> None:
        with SqliteState(self.state_path) as s:
            s.execute(
                f"UPDATE {TABLE} SET stopped_at = ?, updated_at = ?, state = 'stopped'"
                " WHERE engine_id = ?",
                [_iso(now), _iso(now), engine_id],
            )

    def read_all(self) -> list[EngineStatus]:
        """Every engine, by id. Empty before migration 044."""
        with SqliteState(self.state_path) as s:
            if TABLE not in s.tables():
                return []
            rows = s.sql(f"SELECT * FROM {TABLE} ORDER BY engine_id")
        out: list[EngineStatus] = []
        for r in rows:
            try:
                snap = json.loads(r["snapshot_json"] or "{}")
            except ValueError:
                snap = {}
            started = _parse(r["started_at"])
            updated = _parse(r["updated_at"])
            if started is None or updated is None:
                continue
            out.append(
                EngineStatus(
                    engine_id=r["engine_id"],
                    calendar=r["calendar"],
                    state=r["state"],
                    started_at=started,
                    updated_at=updated,
                    stopped_at=_parse(r["stopped_at"]),
                    last_dispatch_at=_parse(r["last_dispatch_at"]),
                    snapshot=snap if isinstance(snap, dict) else {},
                )
            )
        return out


# ---- metrics -------------------------------------------------------------------------


def _labelled(families: Iterable[MetricFamily], extra: Mapping[str, str]) -> list[MetricFamily]:
    return [
        MetricFamily(
            f.name,
            f.help,
            f.type,
            [Sample(s.value, {**s.labels, **extra}, s.suffix) for s in f.samples],
        )
        for f in families
    ]


def _one_engine(status: EngineStatus, now: datetime, stale_after: timedelta) -> list[MetricFamily]:
    eng = {"engine": status.engine_id}
    live = status.is_live(now, stale_after=stale_after)
    driver = status.driver

    def gauge(name: str, help_: str, value: float) -> MetricFamily:
        return MetricFamily(name, help_, "gauge", [Sample(float(value), eng)])

    def counter(name: str, help_: str, key: str) -> MetricFamily:
        return MetricFamily(name, help_, "counter", [Sample(float(driver.get(key) or 0), eng)])

    fams: list[MetricFamily] = []
    health = status.stream
    if health is not None:
        if not live:
            health.state = "stopped"  # a dead engine's last word is not current
        fams += _labelled(stream_metric_families(health), eng)
        if health.last_event_at is not None:
            src = {"source": health.source, **eng}
            age = max((now - health.last_event_at).total_seconds(), 0.0)
            fams.append(
                MetricFamily(
                    "stonks_stream_last_event_age_seconds",
                    "Seconds since the last trade, quote or bar arrived.",
                    "gauge",
                    [Sample(age, src)],
                )
            )
    fams.append(gauge("stonks_engine_up", "1 while the live engine runs and reports.", int(live)))
    fams.append(
        gauge(
            "stonks_engine_last_update_timestamp_seconds",
            "When the engine last wrote its status.",
            status.updated_at.timestamp(),
        )
    )
    if status.last_dispatch_at is not None:
        fams.append(
            gauge(
                "stonks_engine_last_dispatch_timestamp_seconds",
                "When the engine last dispatched a bar close.",
                status.last_dispatch_at.timestamp(),
            )
        )
        fams.append(
            gauge(
                "stonks_engine_last_dispatch_age_seconds",
                "Seconds since the engine last dispatched a bar close.",
                max((now - status.last_dispatch_at).total_seconds(), 0.0),
            )
        )
    fams.append(counter("stonks_engine_events_total", "Events the driver received.", "events"))
    fams.append(counter("stonks_engine_bars_total", "Bars the driver built or took.", "bars"))
    fams.append(counter("stonks_engine_bar_closes_total", "Bar closes dispatched.", "bar_closes"))
    fams.append(
        counter(
            "stonks_engine_late_bars_total",
            "Bars dropped for a close already dispatched.",
            "late_bars",
        )
    )
    fams.append(
        MetricFamily(
            "stonks_engine_handler_errors_total",
            "Bar close handlers that raised, by handler.",
            "counter",
            [
                Sample(float(n), {**eng, "handler": h})
                for h, n in sorted(status.handler_errors().items())
            ],
        )
    )
    fams.append(
        status.latency("dispatch_lag").family(
            "stonks_engine_dispatch_lag_seconds",
            "Clock time from a bar's settle time to its dispatch.",
            eng,
        )
    )
    fams.append(
        status.latency("event_to_order").family(
            "stonks_engine_event_to_order_seconds",
            "Time from a bar close dispatch to the order it caused.",
            eng,
        )
    )
    return fams


def engine_metric_families(
    statuses: Sequence[EngineStatus],
    *,
    now: datetime,
    stale_after: timedelta = DEFAULT_STALE_AFTER,
) -> list[MetricFamily]:
    """Every engine's families, merged so each name appears once."""
    return merge_families(_one_engine(s, now, stale_after) for s in statuses)


def engine_metrics_text(
    state_path: str | Path,
    *,
    now: datetime,
    stale_after: timedelta = DEFAULT_STALE_AFTER,
) -> str:
    """The engine families in the text format, ``""`` with no engine."""
    if not Path(state_path).exists():
        return ""
    statuses = EngineStatusStore(state_path).read_all()
    if not statuses:
        return ""
    return render_prometheus(engine_metric_families(statuses, now=now, stale_after=stale_after))
