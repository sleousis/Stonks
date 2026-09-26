"""The scheduler over HTTP: the schedule and run-now (behind auth),
readiness and liveness probes (public, check names only), and Prometheus
metrics (scrape token or loopback, see :class:`~stonks.api.deps.MetricsAccessConfig`)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from stonks.api.deps import ScopeDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES, problem
from stonks.app.schedule import (
    RECENT_RUNS_MAX,
    ProbeView,
    RunNowRequest,
    RunNowView,
    ScheduleView,
)
from stonks.auth import Permission
from stonks.scheduling.metrics import CONTENT_TYPE

router = APIRouter(prefix="/api/schedule", tags=["schedule"], responses=PROBLEM_RESPONSES)
#: Public probes for orchestrators: status and check names, no details.
probes_router = APIRouter(prefix="/api/health", tags=["health"])
#: ``GET /metrics``, mounted behind :func:`~stonks.api.deps.authorize_metrics`.
metrics_router = APIRouter()

_PROBE_RESPONSES = {503: PROBLEM_RESPONSES[503]}


@router.get("", response_model=ScheduleView, operation_id="getSchedule")
def get_schedule(
    services: ServicesDep,
    limit: Annotated[int, Query(ge=1, le=RECENT_RUNS_MAX, description="recent runs")] = 50,
) -> ScheduleView:
    """Scheduled jobs with their next fire, and the most recent runs."""
    return services.schedule.overview(limit=limit)


@router.post(
    "/{job}/run-now",
    status_code=202,
    response_model=RunNowView,
    operation_id="runScheduledJobNow",
    dependencies=needs(Permission.OPERATIONS_RUN),
)
def run_now(job: str, body: RunNowRequest, services: ServicesDep, scope: ScopeDep) -> RunNowView:
    """Run a scheduled job now (a ``manual:`` run, audited). It runs in the
    background; its row shows up in ``GET /api/schedule``."""
    return services.schedule.run_now(scope, job, body)


def _probe_response(request: Request, view: ProbeView, what: str) -> ProbeView | JSONResponse:
    if view.ok:
        return view
    return problem(request, 503, detail=f"{what}: failing {', '.join(view.failing())}")


@probes_router.get(
    "/live",
    response_model=ProbeView,
    responses=_PROBE_RESPONSES,
    operation_id="getLiveness",
    summary="Liveness probe (process and hosted scheduler)",
)
def live(request: Request, services: ServicesDep) -> ProbeView | JSONResponse:
    return _probe_response(request, services.schedule.liveness(), "not live")


@probes_router.get(
    "/ready",
    response_model=ProbeView,
    responses=_PROBE_RESPONSES,
    operation_id="getReadiness",
    summary="Readiness probe (state migrated, lake present)",
)
def ready(request: Request, services: ServicesDep) -> ProbeView | JSONResponse:
    return _probe_response(request, services.schedule.readiness(), "not ready")


@metrics_router.get("/metrics", include_in_schema=False)
def metrics(services: ServicesDep) -> Response:
    return PlainTextResponse(services.schedule.metrics(), media_type=CONTENT_TYPE)
