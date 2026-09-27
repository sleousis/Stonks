"""The ``local`` backend: jobs run in the scheduler's own process and open
the stores themselves, like the CLI commands do.

For single-user setups where nothing else holds the lake (no ``stonks
serve``). With the API running, use the ``api`` backend instead: DuckDB
allows one writing process per lake file.

The actions call the same services as the CLI:

- ``ingest_prices``: ``IngestPipeline.run_prices`` for the universe over
  the last ``lookback_days`` up to the fire's date;
- ``ingest_metadata``: ``IngestPipeline.run_metadata`` for the universe
  (splits and dividends the tick applies);
- ``tick``: ``run_tick`` through ``build_tick_runtime`` for the fire's date;
- ``health``: ``run_health`` (checks plus the operational halt), alerting
  when unhealthy;
- ``report``: the static HTML report written to ``out``;
- ``backup``: ``run_configured_backup`` (``[backup]`` target and retention);
- ``connections_sync``: every due broker connection synced as
  ``service:scheduler`` (state DB only, so every backend runs it here).

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
    MembersResolver,
    RunContext,
    closed_day_outcome,
    ensure_window,
    job_is_scoped,
    job_universe,
    universes_outcome,
)

LOCAL_ACTIONS = ActionRegistry("local")
register_action = LOCAL_ACTIONS.register


class LocalExecutor(JobExecutor):
    backend = "local"

    def actions(self) -> set[str]:
        return set(LOCAL_ACTIONS.names())

    def execute(self, ctx: RunContext) -> JobOutcome:
        return LOCAL_ACTIONS.get(ctx.spec.action)(ctx)

    def recover(self, settings: Any) -> None:
        """Ticks run in this process: a tick row left ``running`` belongs to
        a scheduler that died, so it is closed as interrupted (TO-06)."""
        from stonks.production.tick import recover_interrupted_ticks
        from stonks.store.state import SqliteState

        if settings is None:
            return
        state = SqliteState(settings.state.path)
        try:
            recover_interrupted_ticks(state)
        finally:
            state.close()


def lake_members(ctx: RunContext) -> MembersResolver:
    """Reads a stored universe's members on a day from the lake."""

    def members(universe_id: str, day: Any) -> list[str]:
        from stonks.production.universe import production_tickers
        from stonks.store.lake import DuckDBLake

        with DuckDBLake(ctx.settings.lake.path) as lake:
            return production_tickers(lake, universe_id, day)

    return members


def build_source(source_id: str, sources: Any) -> Any:
    """Indirection so tests can swap in a fake source."""
    from stonks.ingest.sources.registry import build_source as _build

    return _build(source_id, sources)


@register_action("ingest_prices")
def ingest_prices_action(ctx: RunContext) -> JobOutcome:
    from stonks.ingest.sources.registry import DEFAULT_SOURCE_ID
    from stonks.ingest.wiring import build_ingest_pipeline
    from stonks.store.lake import DuckDBLake

    universe = job_universe(ctx, lake_members(ctx))
    if not universe:
        return JobOutcome("skipped", {"reason": "empty_universe"})
    lookback = int(ctx.params.get("lookback_days", 7))
    source = build_source(str(ctx.params.get("source", DEFAULT_SOURCE_ID)), ctx.settings.sources)
    with DuckDBLake(ctx.settings.lake.path) as lake:
        lake.migrate()
        closed = closed_day_outcome(ctx, universe, lake.get_asset_classes(universe))
        if closed is not None:
            return closed
        result = build_ingest_pipeline(
            ctx.settings, source, lake, source_factory=build_source
        ).run_prices(
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


@register_action("ingest_metadata")
def ingest_metadata_action(ctx: RunContext) -> JobOutcome:
    """Metadata for the universe: splits and dividends among it, which the
    tick applies to held positions (TO-05)."""
    from stonks.ingest.sources.registry import DEFAULT_SOURCE_ID
    from stonks.ingest.wiring import build_ingest_pipeline
    from stonks.store.lake import DuckDBLake

    universe = job_universe(ctx, lake_members(ctx))
    if not universe:
        return JobOutcome("skipped", {"reason": "empty_universe"})
    source = build_source(str(ctx.params.get("source", DEFAULT_SOURCE_ID)), ctx.settings.sources)
    with DuckDBLake(ctx.settings.lake.path) as lake:
        lake.migrate()
        closed = closed_day_outcome(ctx, universe, lake.get_asset_classes(universe))
        if closed is not None:
            return closed
        result = build_ingest_pipeline(
            ctx.settings, source, lake, source_factory=build_source
        ).run_metadata(universe)
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
    from stonks.scheduling.calendar import bars_due
    from stonks.store.lake import DuckDBLake
    from stonks.store.state import SqliteState

    universe = job_universe(ctx, lake_members(ctx))
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
            runtime = build_tick_runtime(
                settings,
                universe,
                scoped=job_is_scoped(ctx),
                bars_due=bars_due(
                    universe, lake.get_asset_classes(universe), ctx.fire.scheduled_for
                ),
            )
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
                    plan=runtime.plan_for(state),
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
    from stonks.production.halts import run_health
    from stonks.store.lake import DuckDBLake
    from stonks.store.state import SqliteState

    settings = ctx.settings
    state = SqliteState(settings.state.path)
    try:
        with DuckDBLake(settings.lake.path) as lake:
            report = run_health(
                state,
                lake,
                job_universe(ctx, _open_lake_members(lake)),
                settings.production.health,
                now=ctx.now,
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


@register_action("backup")
def backup_action(ctx: RunContext) -> JobOutcome:
    """Opens the lake read-only for the copy: only while no other process
    holds it (with ``stonks serve`` running, the ``api`` and ``in_process``
    backends back up inside the server instead)."""
    from stonks.ops.backup import run_configured_backup

    result = run_configured_backup(ctx.settings, now=ctx.now)
    return JobOutcome("succeeded", {"backup_id": result.ref.id, "pruned": result.pruned})


@register_action("connections_sync")
def connections_sync_action(ctx: RunContext) -> JobOutcome:
    """Sync every broker connection whose next sync is due."""
    from stonks.accounts import Scope
    from stonks.connections.service import ConnectionService
    from stonks.connections.settings import ConnectionsConfig
    from stonks.store.state import SqliteState

    state = SqliteState(ctx.settings.state.path)
    try:
        service = ConnectionService(state, ConnectionsConfig.load())
        results = service.sync_due(Scope.service("scheduler"))
    finally:
        state.close()
    failed = [r.connection_id for r in results if not r.ok]
    detail = {"connections": len(results), "failed": failed}
    return JobOutcome("failed" if failed else "succeeded", detail)


def _open_lake_members(lake: Any) -> MembersResolver:
    """Members from a lake this action already holds open."""

    def members(universe_id: str, day: Any) -> list[str]:
        from stonks.production.universe import production_tickers

        return production_tickers(lake, universe_id, day)

    return members


@register_action("universes_refresh")
def universes_refresh_action(ctx: RunContext) -> JobOutcome:
    """Refresh every stored universe, then fetch its members' missing bars
    over the trailing ``ensure_days`` (``[ensure]`` settings)."""
    from stonks.app.lab import build_data_ensurer
    from stonks.core.interval import Interval
    from stonks.ingest.sources.registry import DEFAULT_SOURCE_ID
    from stonks.store.lake import DuckDBLake
    from stonks.universes import UniverseStore, refresh_universe

    settings = ctx.settings
    start, end = ensure_window(ctx)
    interval = Interval.parse(str(ctx.params.get("interval", "1d")))
    source_id = str(ctx.params.get("source") or DEFAULT_SOURCE_ID)
    results: dict[str, dict[str, Any]] = {}
    with DuckDBLake(settings.lake.path) as lake:
        lake.migrate()
        for definition in UniverseStore(lake).list():
            uid = definition.id
            try:
                refreshed = refresh_universe(
                    lake,
                    uid,
                    as_of=ctx.fire.as_of,
                    source_factory=lambda sid: build_source(
                        sid or DEFAULT_SOURCE_ID, settings.sources
                    ),
                )
            except Exception as exc:  # one broken universe must not stop the rest
                results[uid] = {"refresh": "failed", "refresh_error": f"{exc}"}
                continue
            step: dict[str, Any] = {"refresh": "succeeded", "members": refreshed.members}
            if ctx.params.get("ensure", True):
                try:
                    report = build_data_ensurer(
                        settings, lake, build_source(source_id, settings.sources)
                    ).ensure(lake.members_between(uid, start, end), start, end, interval)
                    step |= {
                        "ensure": "succeeded",
                        "tickers_fetched": report.tickers_fetched,
                        "tickers_failed": report.tickers_failed,
                        "warnings": report.warnings,
                    }
                except Exception as exc:
                    step |= {"ensure": "failed", "ensure_error": f"{exc}"}
            results[uid] = step
    return universes_outcome(results)
