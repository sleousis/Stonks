from __future__ import annotations

from fastapi import APIRouter, Response

from stonks.api.deps import ServicesDep
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.api.routers._jobs_common import JOB_CREATED, accepted
from stonks.app.jobs import Job
from stonks.app.lab import BacktestRequest, LabRunRequest

router = APIRouter(prefix="/api/lab", tags=["lab"], responses=PROBLEM_RESPONSES)


@router.post("/backtests", **JOB_CREATED, operation_id="startBacktest")
def start_backtest(body: BacktestRequest, services: ServicesDep, response: Response) -> Job:
    """Queue a backtest; the job result is a ``BacktestResult``."""
    return accepted(services.lab.submit_backtest(body), response)


@router.post("/runs", **JOB_CREATED, operation_id="startLabRun")
def start_lab_run(body: LabRunRequest, services: ServicesDep, response: Response) -> Job:
    """Queue tune → fit → survival suite; the job result is a ``LabRunView``."""
    return accepted(services.lab.submit_lab_run(body), response)
