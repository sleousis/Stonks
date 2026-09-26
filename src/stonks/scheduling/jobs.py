"""Scheduled job specs and the registry of job actions (roadmap 12.2).

A :class:`JobSpec` is a named action plus its trigger and policies, built
from ``[scheduler.jobs]`` by :func:`build_job_specs`. An action is a plain
function ``RunContext -> JobOutcome`` registered by name; the scheduler
never knows what an action does, and an action never knows how it was
triggered. Adding a job kind (backups, syncs) is one ``@register_action``.

The built-in actions call the same services the CLI does:

- ``ingest_prices``: ``IngestPipeline.run_prices`` for the universe over
  the last ``lookback_days`` up to the fire's date;
- ``tick``: ``run_tick`` through ``build_tick_runtime``, for the fire's
  date, skipped when no instrument in the universe trades that day;
- ``health``: ``check_health`` and, when unhealthy, the usual alert;
- ``report``: the static HTML report written to ``out``.

Actions return ``failed`` (rather than raising) when they already sent
their own alert, so the scheduler doesn't alert twice; an exception means
the scheduler alerts.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from stonks.scheduling.calendar import universe_trades_on
from stonks.scheduling.config import (
    CatchUpPolicy,
    DailyTriggerConfig,
    IntervalTriggerConfig,
    SchedulerConfig,
    SessionTriggerConfig,
)
from stonks.scheduling.triggers import (
    DailyTrigger,
    Fire,
    IntervalTrigger,
    SessionTrigger,
    Trigger,
)

if TYPE_CHECKING:
    from stonks.config import Settings
    from stonks.notify import Notifier

#: The actor recorded on every scheduled run (see docs/design/accounts-and-modes.md).
SCHEDULER_ACTOR = "service:scheduler"

JobRunStatus = Literal["succeeded", "skipped", "failed"]


@dataclass(frozen=True)
class JobSpec:
    name: str
    action: str
    trigger: Trigger
    params: Mapping[str, Any] = field(default_factory=dict)
    deadline: timedelta | None = None
    catch_up: CatchUpPolicy = "latest"
    ping_url: str | None = None


@dataclass(frozen=True)
class RunContext:
    spec: JobSpec
    fire: Fire
    run_id: str
    now: datetime
    settings: Settings
    notifier: Notifier

    @property
    def params(self) -> Mapping[str, Any]:
        return self.spec.params


@dataclass(frozen=True)
class JobOutcome:
    status: JobRunStatus
    detail: dict[str, Any] = field(default_factory=dict)
    #: The action already alerted (the tick and health notify on their own).
    alerted: bool = False


JobAction = Callable[[RunContext], JobOutcome]

_ACTIONS: dict[str, JobAction] = {}


class UnknownActionError(KeyError):
    pass


def register_action(name: str) -> Callable[[JobAction], JobAction]:
    def deco(fn: JobAction) -> JobAction:
        _ACTIONS[name] = fn
        return fn

    return deco


def get_action(name: str) -> JobAction:
    try:
        return _ACTIONS[name]
    except KeyError:
        raise UnknownActionError(
            f"unknown scheduler action {name!r}; registered: {sorted(_ACTIONS)}"
        ) from None


def action_names() -> list[str]:
    return sorted(_ACTIONS)


# ---- building specs from config ----------------------------------------------------


def build_trigger(
    cfg: SessionTriggerConfig | DailyTriggerConfig | IntervalTriggerConfig,
) -> Trigger:
    if isinstance(cfg, SessionTriggerConfig):
        return SessionTrigger(
            cfg.calendar, anchor=cfg.anchor, offset=timedelta(minutes=cfg.offset_minutes)
        )
    if isinstance(cfg, DailyTriggerConfig):
        return DailyTrigger(
            cfg.at,
            tz=cfg.timezone,
            weekdays=frozenset(cfg.weekdays) if cfg.weekdays is not None else None,
            calendar=cfg.calendar,
        )
    return IntervalTrigger(timedelta(minutes=cfg.every_minutes))


def build_job_specs(
    config: SchedulerConfig, env: Mapping[str, str] | None = None
) -> list[JobSpec]:
    """Enabled jobs as specs. Fails fast on an unknown action or calendar."""
    env = os.environ if env is None else env
    specs = []
    for job in config.jobs:
        if not job.enabled:
            continue
        get_action(job.action)
        trigger = build_trigger(job.trigger)
        _validate_calendar(trigger)
        ping = job.ping_url.get_secret_value() if job.ping_url else None
        if ping is None and job.ping_url_env:
            ping = env.get(job.ping_url_env) or None
        specs.append(
            JobSpec(
                name=job.name,
                action=job.action,
                trigger=trigger,
                params=dict(job.params),
                deadline=timedelta(minutes=job.deadline_minutes)
                if job.deadline_minutes
                else None,
                catch_up=job.catch_up or config.catch_up,
                ping_url=ping,
            )
        )
    return specs


def _validate_calendar(trigger: Trigger) -> None:
    from stonks.scheduling.calendar import get_calendar

    name = getattr(trigger, "calendar", None)
    if name:
        get_calendar(name)


# ---- built-in actions -------------------------------------------------------------


def _universe(ctx: RunContext) -> list[str]:
    tickers = ctx.params.get("tickers")
    if tickers:
        return [str(t) for t in tickers]
    return list(ctx.settings.production.universe)


def _closed_day(ctx: RunContext, lake: Any, universe: list[str]) -> JobOutcome | None:
    """A ``skipped`` outcome when no instrument in the universe trades on
    the fire's date (``skip_closed_days = false`` in params disables it)."""
    if not ctx.params.get("skip_closed_days", True):
        return None
    classes = lake.get_asset_classes(universe)
    if universe_trades_on(universe, ctx.fire.as_of, classes):
        return None
    return JobOutcome(
        "skipped", {"reason": "market_closed", "as_of": ctx.fire.as_of.isoformat()}
    )


def build_source(source_id: str, sources: Any) -> Any:
    """Indirection so tests can swap in a fake source."""
    from stonks.ingest.sources.registry import build_source as _build

    return _build(source_id, sources)


@register_action("ingest_prices")
def ingest_prices_action(ctx: RunContext) -> JobOutcome:
    from stonks.ingest.pipeline import IngestPipeline
    from stonks.ingest.sources.registry import DEFAULT_SOURCE_ID
    from stonks.store.lake import DuckDBLake

    universe = _universe(ctx)
    if not universe:
        return JobOutcome("skipped", {"reason": "empty_universe"})
    lookback = int(ctx.params.get("lookback_days", 7))
    source = build_source(str(ctx.params.get("source", DEFAULT_SOURCE_ID)), ctx.settings.sources)
    with DuckDBLake(ctx.settings.lake.path) as lake:
        lake.migrate()
        closed = _closed_day(ctx, lake, universe)
        if closed is not None:
            return closed
        result = IngestPipeline(source=source, lake=lake).run_prices(
            universe,
            since=ctx.fire.as_of - timedelta(days=lookback),
            until=ctx.fire.as_of,
        )
    detail = {
        "ingest_run_id": result.run_id,
        "ingest_status": result.status,
        "tickers_ok": result.tickers_ok,
        "tickers_failed": result.tickers_failed,
    }
    return JobOutcome("failed" if result.status == "error" else "succeeded", detail)


@register_action("tick")
def tick_action(ctx: RunContext) -> JobOutcome:
    from stonks.production.settings_builder import build_tick_runtime
    from stonks.production.tick import BackdatedTickError, run_tick
    from stonks.registry.store import StrategyRegistry
    from stonks.store.lake import DuckDBLake
    from stonks.store.state import SqliteState

    universe = _universe(ctx)
    if not universe:
        return JobOutcome("skipped", {"reason": "empty_universe"})
    settings = ctx.settings
    state = SqliteState(settings.state.path)
    try:
        registry = StrategyRegistry(state=state, artifacts_dir=settings.registry.artifacts_dir)
        with DuckDBLake(settings.lake.path) as lake:
            closed = _closed_day(ctx, lake, universe)
            if closed is not None:
                return closed
            runtime = build_tick_runtime(settings, universe)
            try:
                result = run_tick(
                    state=state,
                    lake=lake,
                    registry=registry,
                    settings=runtime.settings,
                    as_of=ctx.fire.as_of,
                    dry_run=bool(ctx.params.get("dry_run", False)),
                    notifier=runtime.notifier,
                    broker_factory=runtime.broker_factory,
                )
            except BackdatedTickError as exc:
                return JobOutcome("skipped", {"reason": "backdated", "error": str(exc)})
            except Exception as exc:
                # run_tick recorded the error row and alerted already.
                return JobOutcome(
                    "failed", {"error": f"{type(exc).__name__}: {exc}"}, alerted=True
                )
    finally:
        state.close()
    detail = {
        "tick_id": result.tick_id,
        "tick_status": result.status,
        "orders_placed": result.orders_placed,
        "fills": result.fills,
    }
    return JobOutcome("failed" if result.status == "error" else "succeeded", detail)


@register_action("health")
def health_action(ctx: RunContext) -> JobOutcome:
    from stonks.production.health import check_health, notify_unhealthy
    from stonks.store.lake import DuckDBLake
    from stonks.store.state import SqliteState

    settings = ctx.settings
    state = SqliteState(settings.state.path)
    try:
        with DuckDBLake(settings.lake.path) as lake:
            report = check_health(
                state, lake, _universe(ctx), settings.production.health, now=ctx.now
            )
    finally:
        state.close()
    failed = [c.name for c in report.failures]
    if report.healthy:
        return JobOutcome("succeeded", {"checks": len(report.checks)})
    notify_unhealthy(report, ctx.notifier)
    return JobOutcome("failed", {"failed_checks": failed}, alerted=True)


@register_action("report")
def report_action(ctx: RunContext) -> JobOutcome:
    from stonks.registry.store import StrategyRegistry
    from stonks.reporting import build_report, render_html
    from stonks.store.state import SqliteState

    settings = ctx.settings
    out = Path(
        ctx.params.get("out") or Path(settings.state.path).parent / "reports" / "latest.html"
    )
    state = SqliteState(settings.state.path)
    try:
        registry = StrategyRegistry(state=state, artifacts_dir=settings.registry.artifacts_dir)
        data = build_report(state, registry, settings.golive, now=ctx.now)
    finally:
        state.close()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(data), encoding="utf-8")
    return JobOutcome("succeeded", {"out": str(out)})
