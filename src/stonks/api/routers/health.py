from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel

from stonks.api.deps import ServicesDep
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.operations import HealthReportView

#: Public liveness probe.
router = APIRouter(prefix="/api/health", tags=["health"])
#: The full report reads the stores, so it sits behind auth like other reads.
report_router = APIRouter(prefix="/api/health", tags=["health"], responses=PROBLEM_RESPONSES)


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
    runs, recent ingest failures. Always 200; see ``healthy``."""
    return services.operations.health_report(tickers)
