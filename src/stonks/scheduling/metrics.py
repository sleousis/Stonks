"""Prometheus metrics and health probes (roadmap 12.3).

Three layers, so any transport can serve them (a FastAPI route, a CLI, a
textfile collector) without this module importing a web framework:

1. :func:`collect_snapshot` reads the state DB (and, optionally, the
   latest bar date per ticker, which the caller gets from the lake with
   :func:`latest_daily_bars`) into a plain :class:`MetricsSnapshot`;
2. :func:`build_metrics` turns a snapshot into metric families (pure);
3. :func:`render_prometheus` renders families in the Prometheus text
   exposition format 0.0.4 (pure).

Probes: :func:`liveness` (the process answers), :func:`readiness` (the
state DB opens and is fully migrated, the lake file exists) and
:func:`scheduler_liveness` (the scheduler's heartbeat is recent).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from stonks.scheduling.jobs import JobSpec
from stonks.scheduling.runs import RunStore
from stonks.store.state import MIGRATIONS_DIR, SqliteState

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

MetricType = Literal["gauge", "counter"]

#: Data-age buckets (inclusive upper bound in days, label).
AGE_BUCKETS: tuple[tuple[int, str], ...] = ((1, "0-1d"), (3, "2-3d"), (7, "4-7d"))
AGE_OVERFLOW = "8d+"
AGE_MISSING = "missing"


@dataclass(frozen=True)
class Sample:
    value: float
    labels: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class MetricFamily:
    name: str
    help: str
    type: MetricType
    samples: Sequence[Sample]


# ---- rendering (pure) ---------------------------------------------------------------


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _escape_help(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n")


def _format_value(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if float(value).is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(float(value))


def render_prometheus(families: Iterable[MetricFamily]) -> str:
    lines: list[str] = []
    for fam in families:
        lines.append(f"# HELP {fam.name} {_escape_help(fam.help)}")
        lines.append(f"# TYPE {fam.name} {fam.type}")
        for s in fam.samples:
            if s.labels:
                labels = ",".join(
                    f'{k}="{_escape_label(str(v))}"' for k, v in sorted(s.labels.items())
                )
                lines.append(f"{fam.name}{{{labels}}} {_format_value(s.value)}")
            else:
                lines.append(f"{fam.name} {_format_value(s.value)}")
    return "\n".join(lines) + "\n"


# ---- snapshot -> families (pure) -----------------------------------------------------


@dataclass(frozen=True)
class MetricsSnapshot:
    now: datetime
    tick_counts: Mapping[str, int] = field(default_factory=dict)
    last_tick_duration_seconds: float | None = None
    last_tick_success: datetime | None = None
    order_counts: Mapping[str, int] = field(default_factory=dict)
    #: ticker -> latest daily bar date (None: no bars). Empty: not collected.
    latest_bars: Mapping[str, date | None] = field(default_factory=dict)
    #: ``jobs`` table (API background jobs) rows by status.
    job_counts: Mapping[str, int] = field(default_factory=dict)
    #: scheduler job name -> ``RunStore.job_summaries`` entry.
    scheduled: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    next_runs: Mapping[str, datetime | None] = field(default_factory=dict)
    scheduler_heartbeat: datetime | None = None


def age_bucket(latest: date | None, today: date) -> str:
    if latest is None:
        return AGE_MISSING
    age = (today - latest).days
    for bound, label in AGE_BUCKETS:
        if age <= bound:
            return label
    return AGE_OVERFLOW


def _ts(when: datetime) -> float:
    return when.timestamp()


def build_metrics(snap: MetricsSnapshot) -> list[MetricFamily]:
    fams: list[MetricFamily] = []

    def add(name: str, help_: str, type_: MetricType, samples: list[Sample]) -> None:
        fams.append(MetricFamily(name, help_, type_, samples))

    add(
        "stonks_tick_runs_total",
        "Production ticks recorded, by status.",
        "counter",
        [Sample(n, {"status": st}) for st, n in sorted(snap.tick_counts.items())],
    )
    if snap.last_tick_duration_seconds is not None:
        add(
            "stonks_tick_duration_seconds",
            "Wall-clock duration of the latest finished tick.",
            "gauge",
            [Sample(snap.last_tick_duration_seconds)],
        )
    if snap.last_tick_success is not None:
        add(
            "stonks_tick_last_success_timestamp_seconds",
            "Unix time the latest ok or partial tick finished.",
            "gauge",
            [Sample(_ts(snap.last_tick_success))],
        )
    add(
        "stonks_orders_total",
        "Orders recorded, by status.",
        "counter",
        [Sample(n, {"status": st}) for st, n in sorted(snap.order_counts.items())],
    )
    add(
        "stonks_order_rejections_total",
        "Orders the broker rejected.",
        "counter",
        [Sample(snap.order_counts.get("rejected", 0))],
    )
    if snap.latest_bars:
        today = snap.now.astimezone(UTC).date()
        buckets = {label: 0 for _, label in AGE_BUCKETS} | {AGE_OVERFLOW: 0, AGE_MISSING: 0}
        for latest in snap.latest_bars.values():
            buckets[age_bucket(latest, today)] += 1
        add(
            "stonks_data_age_tickers",
            "Universe tickers by age of their latest daily bar.",
            "gauge",
            [Sample(n, {"bucket": b}) for b, n in buckets.items()],
        )
        dated = [d for d in snap.latest_bars.values() if d is not None]
        if dated:
            add(
                "stonks_data_oldest_bar_age_days",
                "Age in days of the stalest latest bar in the universe.",
                "gauge",
                [Sample((today - min(dated)).days)],
            )
    add(
        "stonks_jobs",
        "Background jobs (API job runner) by status.",
        "gauge",
        [Sample(n, {"status": st}) for st, n in sorted(snap.job_counts.items())],
    )
    running_scheduled = sum(int(v.get("running", 0)) for v in snap.scheduled.values())
    add(
        "stonks_job_queue_depth",
        "Queued background jobs plus scheduled runs in progress.",
        "gauge",
        [Sample(snap.job_counts.get("queued", 0) + running_scheduled)],
    )
    last_success = [
        Sample(_ts(v["last_success"]), {"job": name})
        for name, v in sorted(snap.scheduled.items())
        if v.get("last_success") is not None
    ]
    add(
        "stonks_scheduled_job_last_success_timestamp_seconds",
        "Unix time each scheduled job last succeeded.",
        "gauge",
        last_success,
    )
    add(
        "stonks_scheduled_job_last_status",
        "1 for the status of each scheduled job's latest run.",
        "gauge",
        [
            Sample(1, {"job": name, "status": str(v["last_status"])})
            for name, v in sorted(snap.scheduled.items())
            if v.get("last_status")
        ],
    )
    add(
        "stonks_scheduled_job_next_run_timestamp_seconds",
        "Unix time of each scheduled job's next fire.",
        "gauge",
        [
            Sample(_ts(at), {"job": name})
            for name, at in sorted(snap.next_runs.items())
            if at is not None
        ],
    )
    if snap.scheduler_heartbeat is not None:
        add(
            "stonks_scheduler_heartbeat_timestamp_seconds",
            "Unix time of the scheduler's latest heartbeat.",
            "gauge",
            [Sample(_ts(snap.scheduler_heartbeat))],
        )
    return fams


# ---- collection ------------------------------------------------------------------


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _counts(state: SqliteState, table: str) -> dict[str, int]:
    if table not in state.tables():
        return {}
    rows = state.sql(f"SELECT status, COUNT(*) AS n FROM {table} GROUP BY status")
    return {r["status"]: int(r["n"]) for r in rows}


def collect_snapshot(
    state_path: str | Path,
    *,
    now: datetime | None = None,
    specs: Sequence[JobSpec] = (),
    latest_bars: Mapping[str, date | None] | None = None,
) -> MetricsSnapshot:
    """Read everything the metrics need from the state DB. Tables that
    don't exist yet (an older DB) simply contribute nothing."""
    now = now or datetime.now(UTC)
    with SqliteState(state_path) as state:
        tables = set(state.tables())
        tick_counts = _counts(state, "tick_runs")
        order_counts = _counts(state, "orders")
        job_counts = _counts(state, "jobs")
        duration = last_success = None
        if "tick_runs" in tables:
            rows = state.sql(
                "SELECT started_at, finished_at FROM tick_runs WHERE finished_at IS NOT NULL "
                "ORDER BY finished_at DESC LIMIT 1"
            )
            if rows:
                start, end = _parse(rows[0]["started_at"]), _parse(rows[0]["finished_at"])
                if start and end:
                    duration = max((end - start).total_seconds(), 0.0)
            rows = state.sql(
                "SELECT MAX(finished_at) AS m FROM tick_runs WHERE status IN ('ok', 'partial')"
            )
            last_success = _parse(rows[0]["m"]) if rows else None
    scheduled: dict[str, Mapping[str, Any]] = {}
    heartbeat = None
    if "scheduled_runs" in tables:
        store = RunStore(state_path)
        scheduled = store.job_summaries()
        inst = store.latest_instance()
        heartbeat = inst["heartbeat_at"] if inst and inst["stopped_at"] is None else None
    next_runs: dict[str, datetime | None] = {}
    for spec in specs:
        fire = spec.trigger.next_fire(now)
        next_runs[spec.name] = fire.scheduled_for if fire else None
    return MetricsSnapshot(
        now=now,
        tick_counts=tick_counts,
        last_tick_duration_seconds=duration,
        last_tick_success=last_success,
        order_counts=order_counts,
        latest_bars=dict(latest_bars or {}),
        job_counts=job_counts,
        scheduled=scheduled,
        next_runs=next_runs,
        scheduler_heartbeat=heartbeat,
    )


def latest_daily_bars(lake: Any, universe: Sequence[str]) -> dict[str, date | None]:
    """ticker -> latest daily bar date (None when it has none)."""
    if not universe:
        return {}
    df = lake.sql(
        "SELECT ticker, MAX(date) AS latest FROM prices WHERE ticker = ANY(?) GROUP BY ticker",
        [list(universe)],
    )
    found: dict[str, date] = {}
    for row in df.itertuples(index=False):
        value = row.latest
        found[row.ticker] = value.date() if hasattr(value, "date") else value
    return {t: found.get(t) for t in universe}


def metrics_text(
    state_path: str | Path,
    *,
    now: datetime | None = None,
    specs: Sequence[JobSpec] = (),
    latest_bars: Mapping[str, date | None] | None = None,
) -> str:
    """Convenience for a ``GET /metrics`` route: collect, build, render."""
    snap = collect_snapshot(state_path, now=now, specs=specs, latest_bars=latest_bars)
    return render_prometheus(build_metrics(snap))


# ---- probes ------------------------------------------------------------------------


@dataclass(frozen=True)
class Probe:
    ok: bool
    checks: dict[str, str]


def liveness() -> Probe:
    """The process is up and answering. Deliberately touches nothing else:
    a liveness failure makes an orchestrator restart the process, which
    wouldn't fix a locked DB."""
    return Probe(True, {"process": "alive"})


def _latest_migration() -> int:
    return max(int(p.stem.split("_", 1)[0]) for p in MIGRATIONS_DIR.glob("*.sql"))


def readiness(state_path: str | Path, lake_path: str | Path | None = None) -> Probe:
    """Ready to serve: the state DB opens and has every migration applied;
    the lake file exists (not opened, the lake allows one writer process)."""
    checks: dict[str, str] = {}
    ok = True
    try:
        with SqliteState(state_path) as state:
            applied = state.applied_migrations()
        want = _latest_migration()
        have = max(applied) if applied else 0
        if have < want:
            ok = False
            checks["state"] = f"migrations behind: {have} < {want} (run stonks db init)"
        else:
            checks["state"] = "ok"
    except Exception as exc:
        ok = False
        checks["state"] = f"error: {type(exc).__name__}: {exc}"
    if lake_path is not None:
        if Path(lake_path).exists():
            checks["lake"] = "ok"
        else:
            ok = False
            checks["lake"] = f"missing: {lake_path}"
    return Probe(ok, checks)


def scheduler_liveness(
    store: RunStore, *, now: datetime, max_silence: timedelta = timedelta(minutes=5)
) -> Probe:
    """The scheduler is running: its latest instance hasn't stopped and
    heartbeated within ``max_silence`` (use a few ``poll_seconds``, plus
    the longest job, since the heartbeat is written between jobs)."""
    inst = store.latest_instance()
    if inst is None:
        return Probe(False, {"scheduler": "never started"})
    if inst["stopped_at"] is not None:
        return Probe(False, {"scheduler": f"stopped at {inst['stopped_at'].isoformat()}"})
    silence = now - inst["heartbeat_at"]
    if silence > max_silence:
        return Probe(False, {"scheduler": f"no heartbeat for {int(silence.total_seconds())}s"})
    return Probe(True, {"scheduler": f"heartbeat {int(silence.total_seconds())}s ago"})
