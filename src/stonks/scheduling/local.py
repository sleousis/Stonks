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
- ``price_alerts``: every person's price alert rules checked against the
  latest closes (roadmap 20.2);
- ``screen_alerts``: every due saved-screen alert run on the day's data,
  names that newly match sent to their owner (roadmap 23.17);
- ``connections_sync``: every due broker connection synced as
  ``service:scheduler`` (state DB only, so every backend runs it here);
- ``broker_health``: probes each IB Gateway, stores its status, alerts and
  pauses auto after a long outage (roadmap 19.4, state DB only);
- ``ibkr_reauth_reminder``: the Sunday push to approve the IBKR login;
- ``model_retrain``: refits strategies that learn from data into
  candidate versions (roadmap 22.6);
- ``live_reconcile``: the reconciliation check (``params.kind``: ``sod``
  or ``eod``) of every portfolio listed on an IB Gateway (roadmap 19.5);
- ``live_stops``: protective stops for the entries the opening auction
  filled, at every live book that turns them on (roadmap 19.10);
- ``live_margin``: the margin cushion of every margin account listed on
  an IB Gateway, with an alert when it is thin (roadmap 19.13);
- ``algo_slices``: sends the due child slices of the parent orders Stonks
  works itself (TWAP and VWAP at a broker without them, roadmap 23.16).

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
    borrow_markets,
    closed_day_outcome,
    ensure_window,
    job_is_scoped,
    job_universe,
    no_gateways,
    retrain_body,
    retrain_outcome,
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


@register_action("ingest_borrow")
def ingest_borrow_action(ctx: RunContext) -> JobOutcome:
    """IBKR's short stock files into ``borrow_rates`` (``stonks ingest
    borrow``, roadmap 19.14). Skipped while no IB Gateway is configured."""
    from stonks.ingest.sources.ibkr_borrow import IbkrBorrowDataSource
    from stonks.ingest.wiring import build_ingest_pipeline
    from stonks.store.lake import DuckDBLake

    markets = borrow_markets(ctx)
    if markets is None:
        return no_gateways()
    source = IbkrBorrowDataSource.from_config(ctx.settings.sources.ibkr_borrow)
    with DuckDBLake(ctx.settings.lake.path) as lake:
        lake.migrate()
        result = build_ingest_pipeline(ctx.settings, source, lake).run_borrow_rates(markets)
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
                    plan=runtime.plan_for(state, dry_run=bool(ctx.params.get("dry_run", False))),
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


@register_action("price_alerts")
def price_alerts_action(ctx: RunContext) -> JobOutcome:
    """Every person's price alert rules against the latest closes, sent
    through the notification router (roadmap 20.2)."""
    from stonks.notify.router import configured_router
    from stonks.price_alerts import run_price_alerts
    from stonks.store.lake import DuckDBLake
    from stonks.store.state import SqliteState

    state = SqliteState(ctx.settings.state.path)
    try:
        with DuckDBLake(ctx.settings.lake.path) as lake:
            out = run_price_alerts(
                state, lake, as_of=ctx.fire.as_of, publish=configured_router(state).publish
            )
    finally:
        state.close()
    return JobOutcome("succeeded", out.as_dict())


@register_action("screen_alerts")
def screen_alerts_action(ctx: RunContext) -> JobOutcome:
    """Every due screen alert of every person, run in this process on the
    lake, sent through the notification router (roadmap 23.17)."""
    from stonks.app.context import AppContext
    from stonks.app.screen_alerts import ScreenAlertService

    out = ScreenAlertService(AppContext(ctx.settings)).evaluate(as_of=ctx.fire.as_of)
    return JobOutcome("succeeded", out.as_dict())


@register_action("price_check")
def price_check_action(ctx: RunContext) -> JobOutcome:
    """The second-source price check before the tick (roadmap 23.6). Skips
    while ``[production.price_check] enabled = false``."""
    from stonks.app.price_checks import PriceCheckRunView, _view, run_configured_price_check
    from stonks.production.price_check import latest_check
    from stonks.store.lake import DuckDBLake
    from stonks.store.state import SqliteState

    if not ctx.settings.production.price_check.enabled:
        return JobOutcome("skipped", {"reason": "disabled"})
    state = SqliteState(ctx.settings.state.path)
    try:
        with DuckDBLake(ctx.settings.lake.path) as lake:
            report, reason = run_configured_price_check(
                ctx.settings, state, lake, ctx.fire.as_of, now=ctx.now
            )
        view = PriceCheckRunView(
            ran=report is not None,
            reason=reason,
            check=_view(latest_check(state, ctx.fire.as_of)) if report is not None else None,
        )
    finally:
        state.close()
    return price_check_outcome(ctx, view.model_dump(mode="json"))


def price_check_outcome(ctx: RunContext, view: dict[str, Any]) -> JobOutcome:
    """A ``PriceCheckRunView`` as JSON onto the run's outcome: skipped, or
    succeeded, with the operator alerted when tickers were held."""
    from stonks.notify import Notification

    if not view.get("ran"):
        return JobOutcome("skipped", {"reason": view.get("reason")})
    check = view.get("check") or {}
    status = check.get("status")
    detail = {
        "status": status,
        "compared": check.get("tickers_compared"),
        "held": check.get("held", []),
    }
    if status not in ("gaps", "systematic"):
        return JobOutcome("succeeded", detail)
    ctx.notifier.notify(
        Notification(
            level="error" if status == "systematic" else "warning",
            title=f"price check: {status}",
            message=f"{check.get('detail')}. Held: {', '.join(check.get('held', [])[:10])}",
            fields=detail,
        )
    )
    return JobOutcome("succeeded", detail, alerted=True)


@register_action("briefings")
def briefings_action(ctx: RunContext) -> JobOutcome:
    """Briefings run the assistant over the API's tools, so only the api
    and in_process backends send them (roadmap 23.8)."""
    return JobOutcome("skipped", {"reason": "briefings run in the API server"})


@register_action("model_retrain")
def model_retrain_action(ctx: RunContext) -> JobOutcome:
    """Refit in this process, opening the stores like the CLI does."""
    from stonks.app.context import AppContext
    from stonks.app.model_versions import ModelVersionService, RetrainRequest
    from stonks.scheduling.jobs import SCHEDULER_ACTOR

    context = AppContext(ctx.settings)
    try:
        result = ModelVersionService(context).retrain(
            RetrainRequest.model_validate(retrain_body(ctx)), actor=SCHEDULER_ACTOR
        )
    finally:
        context.close()
    return retrain_outcome(result.model_dump(mode="json"))


@register_action("lab_verify")
def lab_verify_action(ctx: RunContext) -> JobOutcome:
    """Rerun active strategies from their manifests in this process."""
    from stonks.app.context import AppContext
    from stonks.app.lab import lab_objectives
    from stonks.app.lab_verify import LabVerifyService, VerifyRequest
    from stonks.scheduling.jobs import verify_body, verify_outcome

    context = AppContext(ctx.settings)
    try:
        result = LabVerifyService(context, objectives=lab_objectives()).verify(
            VerifyRequest.model_validate(verify_body(ctx))
        )
    finally:
        context.close()
    return verify_outcome(result.model_dump(mode="json"))


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


@register_action("broker_health")
def broker_health_action(ctx: RunContext) -> JobOutcome:
    """Probe every configured IB Gateway (skipped when none is configured)."""
    from stonks.core.clock import FixedClock
    from stonks.production.broker_health import (
        BrokerProbe,
        IbkrLoginProbe,
        SocketProbe,
        check_gateways,
        gateway_targets,
    )
    from stonks.store.state import SqliteState

    config = ctx.settings.brokers.ibkr
    targets = gateway_targets(config)
    if not targets:
        return JobOutcome("skipped", {"reason": "no_gateways"})
    probe: BrokerProbe = (
        IbkrLoginProbe(config)
        if config.health.probe == "login"
        else SocketProbe(config.health.probe_timeout_seconds)
    )
    state = SqliteState(ctx.settings.state.path)
    try:
        checks = check_gateways(
            state,
            targets,
            probe,
            config.health,
            clock=FixedClock(ctx.now),
        )
    finally:
        state.close()
    down = [c.status.gateway for c in checks if not c.status.connected]
    detail = {
        "gateways": len(checks),
        "down": down,
        "paused": sorted({s for c in checks for s in c.paused}),
    }
    # the job alerted on its own (once a day per gateway)
    return JobOutcome("failed" if down else "succeeded", detail, alerted=bool(down))


@register_action("ibkr_reauth_reminder")
def ibkr_reauth_reminder_action(ctx: RunContext) -> JobOutcome:
    """Remind the owners to approve the weekly IBKR login."""
    from stonks.core.clock import FixedClock
    from stonks.production.broker_health import gateway_targets, send_reauth_reminder
    from stonks.store.state import SqliteState

    targets = gateway_targets(ctx.settings.brokers.ibkr)
    if not targets:
        return JobOutcome("skipped", {"reason": "no_gateways"})
    state = SqliteState(ctx.settings.state.path)
    try:
        sent = send_reauth_reminder(state, targets, clock=FixedClock(ctx.now))
    finally:
        state.close()
    return JobOutcome("succeeded", {"sent": sent})


@register_action("live_submit")
def live_submit_action(ctx: RunContext) -> JobOutcome:
    """Send the approved order tickets due now (roadmap 19.8). It reads the
    real time, not the fire time, so a late run never sends late: tickets
    past their deadline expire instead. Skips while no ticket is open."""
    from stonks.production.settings_builder import submit_broker_opener
    from stonks.production.submit import submit_tickets
    from stonks.production.tickets import open_ticket_count, tickets_recorded
    from stonks.store.state import SqliteState

    state = SqliteState(ctx.settings.state.path)
    try:
        if not tickets_recorded(state) or not open_ticket_count(state):
            return JobOutcome("skipped", {"reason": "no_open_tickets"})
        result = submit_tickets(
            state,
            submit_broker_opener(ctx.settings, state),
            risk=ctx.settings.production.risk,
            live=ctx.settings.production.live,
        )
    finally:
        state.close()
    detail = submit_detail(result)
    return JobOutcome("failed" if result.failed or detail["errors"] else "succeeded", detail)


@register_action("options_live")
def options_live_action(ctx: RunContext) -> JobOutcome:
    """Live options (roadmap 17.8). ``params.phase``: ``plan`` (after the
    close: book assignments, plan expiry closes and rolls as held tickets)
    or ``watch`` (expiry day: alert on a short option still held in or near
    the money). Skips while ``[production.options] live = false``."""
    from stonks.options.live.run import run_options_live
    from stonks.production.settings_builder import submit_broker_opener
    from stonks.store.state import SqliteState

    settings = ctx.settings.production.options
    if not settings.live:
        return JobOutcome("skipped", {"reason": "options_live_off"})
    phase = "watch" if ctx.params.get("phase") == "watch" else "plan"
    state = SqliteState(ctx.settings.state.path)
    try:
        result = run_options_live(
            state,
            submit_broker_opener(ctx.settings, state),
            settings=settings,
            live=ctx.settings.production.live,
            risk=ctx.settings.production.risk,
            as_of=ctx.fire.as_of,
            phase=phase,
        )
    finally:
        state.close()
    detail = result.detail()
    if not result.portfolios:
        return JobOutcome("skipped", {"reason": "no_option_portfolios", **detail})
    return JobOutcome("failed" if result.errors else "succeeded", detail)


@register_action("live_stops")
def live_stops_action(ctx: RunContext) -> JobOutcome:
    """Place, resize and cancel the protective stops of every live book
    after the opening auction (roadmap 19.10), so an entry that filled this
    morning is protected before the evening tick. Skips while no live book
    turns stops on and none has a working stop. The lake is opened read
    only for the ATR. While another process holds it, stops are priced with
    ``fallback_pct`` instead."""
    from stonks.production.live.stops import live_books_of, stops_recorded, sync_live_books
    from stonks.production.settings_builder import (
        build_tick_settings,
        connection_traders,
        submit_broker_opener,
    )
    from stonks.production.tick import load_tick_plan
    from stonks.store.state import SqliteState

    state = SqliteState(ctx.settings.state.path)
    try:
        if not stops_recorded(state):
            return JobOutcome("skipped", {"reason": "no_live_stops"})
        tick_settings = build_tick_settings(ctx.settings, [])
        plan = load_tick_plan(state, tick_settings, traders=connection_traders(state), dry_run=True)
        books = live_books_of(plan, tick_settings)
        working = state.sql(
            "SELECT COUNT(*) AS n FROM orders WHERE protective = 1"
            " AND status IN ('pending', 'partially_filled')"
        )[0]["n"]
        if not any(b.enabled for b in books) and not working:
            return JobOutcome("skipped", {"reason": "no_live_stops"})
        lake = _read_only_lake(ctx.settings)
        try:
            result = sync_live_books(
                state, lake, books, submit_broker_opener(ctx.settings, state), as_of=ctx.fire.as_of
            )
        finally:
            if lake is not None:
                lake.close()
    finally:
        state.close()
    errors = {pid: r["error"] for pid, r in result.items() if "error" in r}
    detail = {"books": result, "errors": errors}
    return JobOutcome("failed" if errors else "succeeded", detail)


def _read_only_lake(settings: Any) -> Any:
    """The lake opened read only, or ``None`` when another process holds it."""
    from stonks.store.lake import DuckDBLake

    try:
        return DuckDBLake(settings.lake.path, read_only=True)
    except Exception:
        return None


@register_action("algo_slices")
def algo_slices_action(ctx: RunContext) -> JobOutcome:
    """Send the child slices that are due, skip those past their window,
    and settle each parent from its children (roadmap 23.16). Skips while
    no parent order is working."""
    from stonks.production.algo_slices import open_parent_count, work_parents
    from stonks.production.settings_builder import submit_broker_opener
    from stonks.store.state import SqliteState

    state = SqliteState(ctx.settings.state.path)
    try:
        if not open_parent_count(state):
            return JobOutcome("skipped", {"reason": "no_working_parents"})
        result = work_parents(state, submit_broker_opener(ctx.settings, state))
    finally:
        state.close()
    detail = result.detail()
    return JobOutcome("failed" if result.failed or result.errors else "succeeded", detail)


def submit_detail(result: Any) -> dict[str, Any]:
    """A job run's detail for a :class:`~stonks.production.submit.SubmitResult`."""
    return {
        "sent": result.sent,
        "failed": result.failed,
        "held": sum(p.held for p in result.portfolios),
        "expired": len(result.expired),
        "settled": result.settled,
        "errors": {
            p.portfolio_id: p.reason for p in result.portfolios if p.status in ("error", "skipped")
        },
    }


@register_action("live_margin")
def live_margin_action(ctx: RunContext) -> JobOutcome:
    """Read every margin account on an IB Gateway and alert when its cushion
    is thin (roadmap 19.13). Skips while no gateway lists a portfolio with
    a margin profile, which is the default."""
    from stonks.core.clock import FixedClock
    from stonks.production.live.margin import margin_portfolios, run_margin_monitor
    from stonks.store.state import SqliteState

    config = ctx.settings.brokers.ibkr
    listed = [pid for gw in config.gateways.values() for pid in gw.portfolios]
    if not listed:
        return JobOutcome("skipped", {"reason": "no_gateways"})
    state = SqliteState(ctx.settings.state.path)
    try:
        if not margin_portfolios(state, listed):
            return JobOutcome("skipped", {"reason": "no_margin_accounts"})
        results = run_margin_monitor(
            state,
            config,
            ctx.settings.production.risk.rules.margin_call,
            clock=FixedClock(ctx.now),
        )
    finally:
        state.close()
    levels = {r.portfolio_id: r.check.level for r in results if r.check is not None}
    unread = sorted(r.portfolio_id for r in results if r.check is None)
    thin = sorted(pid for pid, level in levels.items() if level in ("reduce", "call"))
    detail = {"levels": levels, "unread": unread}
    # each thin account alerted its owner on its own
    return JobOutcome("failed" if thin or unread else "succeeded", detail, alerted=bool(thin))


@register_action("live_reconcile")
def live_reconcile_action(ctx: RunContext) -> JobOutcome:
    """Reconcile every live portfolio against its IB Gateway (skipped when
    no gateway lists a portfolio)."""
    from stonks.core.clock import FixedClock
    from stonks.production.live.checks import check_kind, run_gateway_checks
    from stonks.store.state import SqliteState

    config = ctx.settings.brokers.ibkr
    if not any(gw.portfolios for gw in config.gateways.values()):
        return JobOutcome("skipped", {"reason": "no_gateways"})
    kind = check_kind(str(ctx.params.get("kind", "adhoc")))
    state = SqliteState(ctx.settings.state.path)
    try:
        results = run_gateway_checks(
            state,
            config,
            kind,
            settings=ctx.settings.production.live,
            clock=FixedClock(ctx.now),
            actions_for=_lake_actions(ctx.settings),
            settlement=ctx.settings.production.risk.rules.account_rules,
        )
    finally:
        state.close()
    statuses = {r.report.portfolio_id: r.status for r in results}
    bad = sorted(pid for pid, st in statuses.items() if st in ("drift", "outage", "fault"))
    detail = {"kind": kind, "statuses": statuses, "reports": [r.report.id for r in results]}
    # each check alerted its owner on its own
    return JobOutcome("failed" if bad else "succeeded", detail, alerted=bool(bad))


def _lake_actions(settings: Any) -> Any:
    """Splits for the reconciliation checks, from the lake when it can be
    read (the API may hold it). ``None`` otherwise: ownership then counts
    raw fills."""

    def actions_for(tickers: Any) -> Any:
        from stonks.production.corporate_actions import load_corporate_actions
        from stonks.store.lake import DuckDBLake

        if not tickers:
            return None
        try:
            with DuckDBLake(settings.lake.path, read_only=True) as lake:
                return load_corporate_actions(lake, tickers)
        except Exception as exc:
            from stonks.logging import get_logger

            get_logger("stonks.scheduling.local").info("live_reconcile.no_lake", error=str(exc))
            return None

    return actions_for


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


@register_action("calendars_refresh")
def calendars_refresh_action(ctx: RunContext) -> JobOutcome:
    """The event calendars and the upcoming-event notifications, in this
    process (roadmap 20.7)."""
    from stonks.app.calendars import CalendarRefreshRequest
    from stonks.calendars.alerts import check_event_alerts
    from stonks.ingest.wiring import build_ingest_pipeline
    from stonks.notify.router import configured_router
    from stonks.scheduling.api_backend import calendar_job_outcome, calendar_refresh_body
    from stonks.store.lake import DuckDBLake
    from stonks.store.state import SqliteState

    request = CalendarRefreshRequest.model_validate(calendar_refresh_body(ctx))
    assert request.start is not None and request.end is not None
    source = build_source(request.source, ctx.settings.sources)
    with DuckDBLake(ctx.settings.lake.path) as lake:
        lake.migrate()
        result = build_ingest_pipeline(ctx.settings, source, lake).run_calendars(
            request.start,
            request.end,
            tickers=request.tickers,
            countries=request.countries,
            kinds=request.kinds,
        )
        view: dict[str, Any] = {
            "run_id": result.run_id,
            "status": result.status,
            "calendars_ok": result.tickers_ok,
            "calendars_failed": result.tickers_failed,
        }
        if request.alerts:
            with SqliteState(ctx.settings.state.path) as state:
                report = check_event_alerts(
                    lake, configured_router(state), ctx.fire.as_of, days_ahead=request.alert_days
                )
            view["alerts"] = {"sent": report.sent}
    return calendar_job_outcome("succeeded", None, view, f"ingest_run_{result.run_id}")


@register_action("live_gate_days")
def live_gate_days_action(ctx: RunContext) -> JobOutcome:
    """Record the session's gate metrics for every portfolio in
    ``broker_paper`` or higher (roadmap 19.9) and alert the owner of a week
    that was not clean. It never changes a stage or an allocation. Skips
    while no portfolio is past ``sim_paper``."""
    from stonks.core.clock import FixedClock
    from stonks.production.live.gates import live_portfolios, owner_alert, record_gate_days
    from stonks.store.state import SqliteState

    state = SqliteState(ctx.settings.state.path)
    try:
        if not live_portfolios(state):
            return JobOutcome("skipped", {"reason": "no_live_portfolios"})
        run = record_gate_days(
            state,
            ctx.fire.as_of,
            settings=ctx.settings.production.live.stages,
            alert=owner_alert(state),
            clock=FixedClock(ctx.now),
        )
    finally:
        state.close()
    return JobOutcome(
        "succeeded",
        {
            "session": run.day.isoformat(),
            "recorded": len(run.recorded),
            "clean": sum(1 for g in run.recorded if g.clean),
            "dirty_weeks": list(run.dirty_weeks),
        },
    )


@register_action("engine_start")
def engine_start_action(ctx: RunContext) -> JobOutcome:
    """The engine is its own process, controlled through files next to the
    state DB, so every backend starts it the same way (roadmap 21.2.5)."""
    from stonks.scheduling.jobs import engine_start_job

    return engine_start_job(ctx)


@register_action("engine_stop")
def engine_stop_action(ctx: RunContext) -> JobOutcome:
    """Every backend stops the engine the same way (roadmap 21.2.5)."""
    from stonks.scheduling.jobs import engine_stop_job

    return engine_stop_job(ctx)
