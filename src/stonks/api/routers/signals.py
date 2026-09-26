"""Signal research routes (BL-33): queue a signal IC analysis, then read
its typed result."""

from __future__ import annotations

from fastapi import APIRouter, Response

from stonks.api.deps import ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.api.routers._jobs_common import JOB_CREATED, accepted
from stonks.app.jobs import Job
from stonks.app.signals import SIGNAL_IC_JOB, SignalICRequest, SignalICView
from stonks.auth import Permission

router = APIRouter(prefix="/api/lab", tags=["lab"], responses=PROBLEM_RESPONSES)


@router.post(
    "/signal-ic",
    **JOB_CREATED,
    operation_id="startSignalIc",
    dependencies=needs(Permission.LAB_RUN),
)
def start_signal_ic(body: SignalICRequest, services: ServicesDep, response: Response) -> Job:
    """Queue a signal IC analysis of a strategy's ``estimate_return`` (mean
    IC, ICIR, HAC t-stat, decay, quantile spread, turnover); fetch it from
    ``GET /api/lab/signal-ic/{job_id}/result`` once it succeeds. Universes
    under 10 tickers come back ``n/a``."""
    return accepted(services.signals.submit_signal_ic(body), response)


@router.get(
    "/signal-ic/{job_id}/result", response_model=SignalICView, operation_id="getSignalIcResult"
)
def get_signal_ic_result(job_id: str, services: ServicesDep) -> SignalICView:
    """The result of a succeeded signal IC job (409 until it has succeeded)."""
    return services.jobs.typed_result(job_id, SIGNAL_IC_JOB, SignalICView)
