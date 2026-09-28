from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel

from stonks.api.deps import PrincipalDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.operations import HealthReportView
from stonks.app.price_checks import PriceCheckRunBody, PriceCheckRunView, PriceCheckView
from stonks.auth import Permission

#: Public liveness probe.
router = APIRouter(prefix="/api/health", tags=["health"])
#: The full report reads the stores, so it sits behind auth like other reads.
report_router = APIRouter(prefix="/api/health", tags=["health"], responses=PROBLEM_RESPONSES)


class HealthRunRequest(BaseModel):
    #: Freshness check tickers; default ``[production].universe``.
    tickers: list[str] | None = None


class Health(BaseModel):
    status: str
    version: str


def _version() -> str:
    try:
        return version("stonks")
    except PackageNotFoundError:  # pragma: no cover - source checkout without install
        return "0.0.0"


@router.get("", response_model=Health, operation_id="getHealth", summary="Liveness probe")
def health() -> Health:
    return Health(status="ok", version=_version())


@report_router.get("/report", response_model=HealthReportView, operation_id="getHealthReport")
def health_report(
    services: ServicesDep,
    tickers: Annotated[
        list[str] | None,
        Query(description="freshness check tickers; default [production].universe"),
    ] = None,
) -> HealthReportView:
    """Every ``stonks health`` check: bar freshness, stuck ticks and ingest
    runs, recent ingest failures, open halts. Always 200; see ``healthy``.
    Read-only: it never opens or clears the operational halt."""
    return services.operations.health_report(tickers)


@report_router.post(
    "/run",
    response_model=HealthReportView,
    operation_id="runHealthChecks",
    dependencies=needs(Permission.OPERATIONS_RUN),
)
def run_health_checks(
    body: HealthRunRequest, services: ServicesDep, principal: PrincipalDep
) -> HealthReportView:
    """The scheduled health job: every check, then the global operational
    halt is opened on stale data or a stuck run and cleared when the checks
    pass again. The caller is recorded as the actor."""
    return services.operations.run_health(body.tickers, actor=principal.scope.actor)


@report_router.get(
    "/price-check",
    response_model=PriceCheckView | None,
    operation_id="getPriceCheck",
)
def latest_price_check(services: ServicesDep) -> PriceCheckView | None:
    """The newest second-source price check (roadmap 23.6): each held and
    signalled ticker's vendor close and adjusted return against a second
    source, and the tickers whose opening orders the tick holds. Null
    before the first check."""
    return services.price_checks.latest()


@report_router.post(
    "/price-check/run",
    response_model=PriceCheckRunView,
    operation_id="runPriceCheck",
    dependencies=needs(Permission.OPERATIONS_RUN),
)
def run_price_check(body: PriceCheckRunBody, services: ServicesDep) -> PriceCheckRunView:
    """The scheduled price check job: compare now and store the result. A
    systematic gap opens the global operational halt. Skips while
    ``[production.price_check] enabled = false``."""
    return services.price_checks.run(body.as_of)
