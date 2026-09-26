"""One router module per domain. Adding a domain = one module + one entry
in :data:`API_ROUTERS`; every entry is mounted behind :func:`authorize`."""

from __future__ import annotations

from fastapi import APIRouter

from stonks.api.routers import (
    catalog,
    health,
    ingest,
    jobs,
    lab,
    market,
    orders,
    portfolio,
    sources,
    strategies,
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
    sources.router,
]

#: Routers that run their own auth dependency instead of :func:`authorize`
#: (the job event stream also accepts a job-scoped ``?token=``).
STREAM_ROUTERS: list[APIRouter] = [
    jobs.events_router,
]

#: Routers that stay open (liveness probes).
PUBLIC_ROUTERS: list[APIRouter] = [health.router]
