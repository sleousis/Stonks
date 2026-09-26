"""The ``local`` backend: jobs run in the scheduler's own process and open
the stores themselves, like the CLI commands do.

For single-user setups where nothing else holds the lake (no ``stonks
serve``). With the API running, use the ``api`` backend instead: DuckDB
allows one writing process per lake file.

The actions call the same services as the CLI:

- ``ingest_prices``: ``IngestPipeline.run_prices`` for the universe over
  the last ``lookback_days`` up to the fire's date;
- ``tick``: ``run_tick`` through ``build_tick_runtime`` for the fire's date;
- ``health``: ``check_health``, alerting when unhealthy;
- ``report``: the static HTML report written to ``out``.

``ingest_prices`` and ``tick`` are skipped when no instrument in the
universe trades on the fire's date (asset classes read from the lake).
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from stonks.scheduling.jobs import (
    ActionRegistry,
    JobExecutor,
    JobOutcome,
    RunContext,
    closed_day_outcome,
    job_universe,
)

LOCAL_ACTIONS = ActionRegistry("local")
register_action = LOCAL_ACTIONS.register


class LocalExecutor(JobExecutor):
    backend = "local"

    def actions(self) -> set[str]:
        return set(LOCAL_ACTIONS.names())

    def execute(self, ctx: RunContext) -> JobOutcome:
        return LOCAL_ACTIONS.get(ctx.spec.action)(ctx)


def build_source(source_id: str, sources: Any) -> Any:
    """Indirection so tests can swap in a fake source."""
    from stonks.ingest.sources.registry import build_source as _build

    return _build(source_id, sources)


@register_action("ingest_prices")
def ingest_prices_action(ctx: RunContext) -> JobOutcome:
    from stonks.ingest.pipeline import IngestPipeline
    from stonks.ingest.sources.registry import DEFAULT_SOURCE_ID
    from stonks.store.lake import DuckDBLake

    universe = job_universe(ctx)
    if not universe:
        return JobOutcome("skipped", {"reason": "empty_universe"})
    lookback = int(ctx.params.get("lookback_days", 7))
    source = build_source(str(ctx.params.get("source", DEFAULT_SOURCE_ID)), ctx.settings.sources)
    with DuckDBLake(ctx.settings.lake.path) as lake:
        lake.migrate()
        closed = closed_day_outcome(ctx, universe, lake.get_asset_classes(universe))
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

    universe = job_universe(ctx)
    if not universe:
        return JobOutcome("skipped", {"reason": "empty_universe"})
    settings = ctx.settings
    state = SqliteState(settings.state.path)
    try:
        registry = StrategyRegistry(state=state, artifacts_dir=settings.registry.artifacts_dir)
        with DuckDBLake(settings.lake.path) as lake:
            closed = closed_day_outcome(ctx, universe, lake.get_asset_classes(universe))
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
                return JobOutcome("failed", {"error": f"{type(exc).__name__}: {exc}"}, alerted=True)
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
    from stonks.production.health import check_health
    from stonks.store.lake import DuckDBLake
    from stonks.store.state import SqliteState

    settings = ctx.settings
    state = SqliteState(settings.state.path)
    try:
        with DuckDBLake(settings.lake.path) as lake:
            report = check_health(
                state, lake, job_universe(ctx), settings.production.health, now=ctx.now
            )
    finally:
        state.close()
    return health_outcome(ctx, report)


def health_outcome(ctx: RunContext, report: Any) -> JobOutcome:
    """``succeeded`` or, after the usual health alert, ``failed``."""
    from stonks.production.health import notify_unhealthy

    if report.healthy:
        return JobOutcome("succeeded", {"checks": len(report.checks)})
    notify_unhealthy(report, ctx.notifier)
    return JobOutcome("failed", {"failed_checks": [c.name for c in report.failures]}, alerted=True)


@register_action("report")
def report_action(ctx: RunContext) -> JobOutcome:
    """Reads the state DB only, so every backend runs it in-process."""
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
