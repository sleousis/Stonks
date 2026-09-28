"""One router module per domain. Adding a domain = one module + one entry
in :data:`API_ROUTERS`; every entry is mounted behind :func:`authorize`."""

from __future__ import annotations

from fastapi import APIRouter

from stonks.api.routers import (
    alerts,
    assistant,
    auth,
    backups,
    brokers,
    calendars,
    cash_flows,
    catalog,
    charts,
    connections,
    exports,
    factors,
    golive,
    halts,
    health,
    ingest,
    insights,
    jobs,
    journal,
    lab,
    lab_worker,
    ledger,
    live,
    manual_orders,
    market,
    model_versions,
    notifications,
    onboarding,
    options,
    order_drafts,
    orders,
    pnl,
    portfolio,
    portfolios,
    price_alerts,
    reconcile,
    risk,
    schedule,
    screener,
    shadow,
    signals,
    sources,
    statements,
    strategies,
    stream,
    studio,
    tax,
    tca,
    telegram,
    tickets,
    ticks,
    universes,
    watchlists,
)

#: Routers mounted behind the auth dependency.
API_ROUTERS: list[APIRouter] = [
    portfolio.router,
    portfolios.router,
    portfolios.subscriptions_router,
    cash_flows.router,
    strategies.router,
    golive.router,
    model_versions.router,
    market.router,
    ingest.router,
    orders.router,
    order_drafts.router,
    tickets.router,
    manual_orders.router,
    ticks.router,
    lab.router,
    lab_worker.router,
    ledger.router,
    catalog.router,
    jobs.router,
    studio.router,
    sources.router,
    risk.router,
    halts.router,
    reconcile.router,
    shadow.router,
    pnl.router,
    health.report_router,
    brokers.router,
    alerts.router,
    connections.router,
    notifications.push_router,
    notifications.router,
    schedule.router,
    signals.router,
    factors.router,
    options.router,
    universes.router,
    statements.router,
    backups.router,
    tca.router,
    journal.router,
    insights.router,
    onboarding.router,
    watchlists.router,
    price_alerts.router,
    charts.router,
    exports.router,
    assistant.router,
    telegram.router,
    tax.router,
    tax.fx_router,
    live.router,
    calendars.router,
    screener.router,
    stream.router,
]

#: Routers that always need a principal, even for reads on loopback.
TOKEN_ROUTERS: list[APIRouter] = [auth.router]

#: Routers that run their own auth dependency instead of :func:`authorize`
#: (the job event stream also accepts a job-scoped ``?token=``).
STREAM_ROUTERS: list[APIRouter] = [
    jobs.events_router,
]

#: Routers that stay open (liveness and readiness probes, and sign-in, which
#: checks its own credentials).
PUBLIC_ROUTERS: list[APIRouter] = [health.router, schedule.probes_router, auth.public_router]

#: ``GET /metrics``: the scrape token or a loopback peer (``authorize_metrics``).
METRICS_ROUTERS: list[APIRouter] = [schedule.metrics_router]
