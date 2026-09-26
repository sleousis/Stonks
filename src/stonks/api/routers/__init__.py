"""One router module per domain. Adding a domain = one module + one entry
in :data:`API_ROUTERS`; every entry is mounted behind :func:`authorize`."""

from __future__ import annotations

from fastapi import APIRouter

from stonks.api.routers import (
    alerts,
    auth,
    brokers,
    catalog,
    health,
    ingest,
    jobs,
    lab,
    market,
    orders,
    pnl,
    portfolio,
    risk,
    shadow,
    sources,
    strategies,
    studio,
    ticks,
)

#: Routers mounted behind the auth dependency.
API_ROUTERS: list[APIRouter] = [
    portfolio.router,
    strategies.router,
    market.router,
    ingest.router,
    orders.router,
    ticks.router,
    lab.router,
    catalog.router,
    jobs.router,
    studio.router,
    sources.router,
    risk.router,
    shadow.router,
    pnl.router,
    health.report_router,
    brokers.router,
    alerts.router,
]

#: Routers that always need the bearer token, even for reads on loopback.
TOKEN_ROUTERS: list[APIRouter] = [auth.router]

#: Routers that run their own auth dependency instead of :func:`authorize`
#: (the job event stream also accepts a job-scoped ``?token=``).
STREAM_ROUTERS: list[APIRouter] = [
    jobs.events_router,
]

#: Routers that stay open (liveness probes).
PUBLIC_ROUTERS: list[APIRouter] = [health.router]
