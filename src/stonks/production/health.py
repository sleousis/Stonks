"""Operational health checks behind ``stonks health`` (roadmap 2.5b).

Checks:

- ``freshness:<ticker>``: latest daily bar is at most ``max_bar_age_days``
  calendar days old (no bars at all is unhealthy);
- ``stuck_ticks``: no ``tick_runs`` row sits in ``running`` longer than
  ``stuck_tick_minutes`` (a crashed or hung tick);
- ``stuck_ingest_runs``: same for ``ingest_runs`` with ``stuck_ingest_minutes``;
- ``ingest_failures``: no ingest run with status ``error`` started within
  ``ingest_failure_lookback_hours``.

A check that itself crashes (missing table, locked file) is reported as
failed rather than raised: a health command that dies is not a healthy one.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from stonks.config import HealthConfig
from stonks.logging import get_logger
from stonks.notify import Notification, Notifier
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState

_log = get_logger("stonks.production.health")


@dataclass(frozen=True)
class HealthCheck:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class HealthReport:
    checks: list[HealthCheck]
    checked_at: datetime

    @property
    def healthy(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def failures(self) -> list[HealthCheck]:
        return [c for c in self.checks if not c.ok]


def check_health(
    state: SqliteState,
    lake: DuckDBLake,
    universe: Sequence[str],
    config: HealthConfig,
    now: datetime | None = None,
) -> HealthReport:
    now = now or datetime.now(UTC)
    checks: list[HealthCheck] = []
    checks.extend(_guard("freshness", lambda: _freshness(lake, universe, config, now)))
    checks.extend(_guard("stuck_ticks", lambda: [_stuck_ticks(state, config, now)]))
    checks.extend(_guard("stuck_ingest_runs", lambda: [_stuck_ingest(lake, config, now)]))
    checks.extend(_guard("ingest_failures", lambda: [_ingest_failures(lake, config, now)]))
    checks.extend(_guard("var_violations", lambda: [_var_violations(state, now)]))
    checks.extend(_guard("lab_queue", lambda: [_lab_queue(state, config, now)]))
    report = HealthReport(checks=checks, checked_at=now)
    _log.info(
        "health.checked",
        healthy=report.healthy,
        failed=[c.name for c in report.failures],
    )
    return report


def notify_unhealthy(report: HealthReport, notifier: Notifier) -> None:
    if report.healthy:
        return
    failed = [c.name for c in report.failures]
    notifier.notify(
        Notification(
            level="error",
            title="stonks health check failed",
            message="; ".join(f"{c.name}: {c.detail}" for c in report.failures),
            fields={"failed_checks": failed, "checked_at": report.checked_at.isoformat()},
        )
    )


# ---- individual checks -----------------------------------------------------


def _guard(name: str, fn: Callable[[], list[HealthCheck]]) -> list[HealthCheck]:
    try:
        return fn()
    except Exception as exc:
        _log.error("health.check_crashed", check=name, error=str(exc))
        return [HealthCheck(name=name, ok=False, detail=f"check error: {exc}")]


def _freshness(
    lake: DuckDBLake, universe: Sequence[str], config: HealthConfig, now: datetime
) -> list[HealthCheck]:
    if not universe:
        return []
    df = lake.sql(
        "SELECT ticker, MAX(date) AS latest FROM prices WHERE ticker = ANY(?) GROUP BY ticker",
        [list(universe)],
    )
    latest: dict[str, date] = {
        row.ticker: _as_date(row.latest) for row in df.itertuples(index=False)
    }
    today = now.astimezone(UTC).date()
    checks = []
    for ticker in universe:
        name = f"freshness:{ticker}"
        last = latest.get(ticker)
        if last is None:
            checks.append(HealthCheck(name=name, ok=False, detail="no daily bars"))
            continue
        age = (today - last).days
        ok = age <= config.max_bar_age_days
        checks.append(
            HealthCheck(
                name=name,
                ok=ok,
                detail=f"latest bar {last.isoformat()} ({age}d old, max {config.max_bar_age_days}d)",
            )
        )
    return checks


def _stuck_ticks(state: SqliteState, config: HealthConfig, now: datetime) -> HealthCheck:
    cutoff = now - timedelta(minutes=config.stuck_tick_minutes)
    rows = state.sql("SELECT id, started_at FROM tick_runs WHERE status = 'running'")
    stuck = [r["id"] for r in rows if _parse_iso(r["started_at"]) < cutoff]
    if stuck:
        return HealthCheck(
            name="stuck_ticks",
            ok=False,
            detail=f"running > {config.stuck_tick_minutes}m: {', '.join(stuck)}",
        )
    return HealthCheck(name="stuck_ticks", ok=True, detail="none")


# ``ingest_runs`` timestamps are naive TIMESTAMPs that DuckDB wrote in the
# session time zone, so ages are computed in SQL via TIMESTAMPTZ casts
# against a tz-aware ``now`` rather than compared as naive datetimes.
_AGE_MINUTES = "(epoch(CAST(? AS TIMESTAMPTZ)) - epoch(CAST(started_at AS TIMESTAMPTZ))) / 60.0"


def _stuck_ingest(lake: DuckDBLake, config: HealthConfig, now: datetime) -> HealthCheck:
    df = lake.sql(
        f"SELECT id, kind FROM ingest_runs WHERE status = 'running' AND {_AGE_MINUTES} > ? "
        "ORDER BY id",
        [now.isoformat(), config.stuck_ingest_minutes],
    )
    if len(df):
        runs = ", ".join(f"#{r.id} ({r.kind})" for r in df.itertuples(index=False))
        return HealthCheck(
            name="stuck_ingest_runs",
            ok=False,
            detail=f"running > {config.stuck_ingest_minutes}m: {runs}",
        )
    return HealthCheck(name="stuck_ingest_runs", ok=True, detail="none")


def _ingest_failures(lake: DuckDBLake, config: HealthConfig, now: datetime) -> HealthCheck:
    df = lake.sql(
        f"SELECT id, kind, error FROM ingest_runs WHERE status = 'error' AND {_AGE_MINUTES} <= ? "
        "ORDER BY id",
        [now.isoformat(), config.ingest_failure_lookback_hours * 60],
    )
    if len(df):
        runs = ", ".join(f"#{r.id} ({r.kind})" for r in df.itertuples(index=False))
        return HealthCheck(
            name="ingest_failures",
            ok=False,
            detail=f"failed in last {config.ingest_failure_lookback_hours}h: {runs}",
        )
    return HealthCheck(name="ingest_failures", ok=True, detail="none")


def _var_violations(state: SqliteState, now: datetime) -> HealthCheck:
    """BL-47: warns when a portfolio's rolling 95% VaR violation ratio is
    outside the band (the risk model is off), once enough days are scored."""
    from stonks.production.risk_metrics import (
        RiskMonitorSettings,
        latest_portfolio_rows,
        risk_snapshots_enabled,
    )

    if not risk_snapshots_enabled(state):
        return HealthCheck(name="var_violations", ok=True, detail="no risk_snapshots table")
    settings = RiskMonitorSettings()
    off = [
        f"{r.portfolio_id} ratio {r.violation_ratio_95:.2f} over {r.window_days}d"
        for r in latest_portfolio_rows(state, now.astimezone(UTC).date())
        if r.ratio_out_of_band(settings)
    ]
    if off:
        band = f"{settings.ratio_low:g}-{settings.ratio_high:g}"
        return HealthCheck(
            name="var_violations", ok=False, detail=f"outside {band}: {', '.join(off)}"
        )
    return HealthCheck(name="var_violations", ok=True, detail="within band or too few days")


def _lab_queue(state: SqliteState, config: HealthConfig, now: datetime) -> HealthCheck:
    """Roadmap 14.9: lab worker jobs wait with no live worker, or lost one."""
    from stonks.lab.offload.health import queue_health

    if "lab_workers" not in state.tables():
        return HealthCheck(name="lab_queue", ok=True, detail="no lab queue table")
    ok, detail = queue_health(state, stuck_minutes=config.stuck_lab_queue_minutes, now=now)
    return HealthCheck(name="lab_queue", ok=ok, detail=detail)


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _as_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    # pandas Timestamp
    return value.date()  # type: ignore[attr-defined]
