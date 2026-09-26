from __future__ import annotations

from fastapi import APIRouter, Response

from stonks.api.deps import PrincipalDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.api.routers._jobs_common import JOB_CREATED, accepted
from stonks.app.jobs import Job
from stonks.app.lab import (
    BACKTEST_JOB,
    LAB_ENSURE_JOB,
    LAB_RUN_JOB,
    LAB_SWEEP_JOB,
    BacktestRequest,
    BacktestResult,
    CostModelPreset,
    LabRunRequest,
    LabRunView,
    SurvivalPresetInfo,
    SurvivalTestInfo,
    survival_preset_catalog,
    survival_test_catalog,
)
from stonks.app.sweep import SweepRequest, SweepResultView
from stonks.auth import Permission, require
from stonks.ingest.ensure import EnsureReport

router = APIRouter(prefix="/api/lab", tags=["lab"], responses=PROBLEM_RESPONSES)


@router.post(
    "/backtests",
    **JOB_CREATED,
    operation_id="startBacktest",
    dependencies=needs(Permission.LAB_RUN),
)
def start_backtest(body: BacktestRequest, services: ServicesDep, response: Response) -> Job:
    """Queue a backtest; fetch the typed result from
    ``GET /api/lab/backtests/{job_id}/result`` once it succeeds."""
    return accepted(services.lab.submit_backtest(body), response)


@router.post(
    "/runs", **JOB_CREATED, operation_id="startLabRun", dependencies=needs(Permission.LAB_RUN)
)
def start_lab_run(
    body: LabRunRequest, services: ServicesDep, principal: PrincipalDep, response: Response
) -> Job:
    """Queue tune → fit → survival suite; fetch the typed result from
    ``GET /api/lab/runs/{job_id}/result`` once it succeeds. Registering the
    result in the catalog (``register_strategy``, ``register_if_passes``)
    needs an admin."""
    if body.registers:
        require(principal, Permission.STRATEGY_PROMOTE)
    return accepted(services.lab.submit_lab_run(body), response)


@router.get(
    "/backtests/{job_id}/result",
    response_model=BacktestResult,
    operation_id="getBacktestResult",
)
def get_backtest_result(job_id: str, services: ServicesDep) -> BacktestResult:
    """The result of a succeeded backtest job (409 until it has succeeded)."""
    return services.jobs.typed_result(job_id, BACKTEST_JOB, BacktestResult)


@router.get("/runs/{job_id}/result", response_model=LabRunView, operation_id="getLabRunResult")
def get_lab_run_result(job_id: str, services: ServicesDep) -> LabRunView:
    """The result of a succeeded lab-run job (409 until it has succeeded)."""
    return services.jobs.typed_result(job_id, LAB_RUN_JOB, LabRunView)


@router.post(
    "/sweeps", **JOB_CREATED, operation_id="startSweep", dependencies=needs(Permission.LAB_RUN)
)
def start_sweep(body: SweepRequest, services: ServicesDep, response: Response) -> Job:
    """Queue a sweep: every catalogued strategy (or ``strategies``) through
    the lab on one basket. Nothing is registered; every run lands in the
    trial ledger. Fetch the rows from ``GET /api/lab/sweeps/{job_id}/result``."""
    return accepted(services.lab.submit_sweep(body), response)


@router.get(
    "/sweeps/{job_id}/result", response_model=SweepResultView, operation_id="getSweepResult"
)
def get_sweep_result(job_id: str, services: ServicesDep) -> SweepResultView:
    """The rows of a succeeded sweep job (409 until it has succeeded)."""
    return services.jobs.typed_result(job_id, LAB_SWEEP_JOB, SweepResultView)


@router.get(
    "/survival-tests", response_model=list[SurvivalTestInfo], operation_id="listSurvivalTests"
)
def list_survival_tests() -> list[SurvivalTestInfo]:
    """Every survival test with the JSON Schema of its options (what
    ``test_options[id]`` on a lab run may set) and the presets that run it."""
    return survival_test_catalog()


@router.get(
    "/survival-presets",
    response_model=list[SurvivalPresetInfo],
    operation_id="listSurvivalPresets",
)
def list_survival_presets() -> list[SurvivalPresetInfo]:
    """The named suites, their tests and the options they give them."""
    return survival_preset_catalog()


@router.get(
    "/ensure/{job_id}/result", response_model=EnsureReport, operation_id="getLabEnsureResult"
)
def get_lab_ensure_result(job_id: str, services: ServicesDep) -> EnsureReport:
    """The report of a lab run's chained data job (``ensure_data``; the
    run's ``ensure_job_id``), 409 until it has succeeded."""
    return services.jobs.typed_result(job_id, LAB_ENSURE_JOB, EnsureReport)


@router.get("/cost-models", response_model=list[CostModelPreset], operation_id="listCostModels")
def list_cost_models(services: ServicesDep) -> list[CostModelPreset]:
    """Transaction-cost presets a backtest request can name in ``cost_model``."""
    return services.lab.cost_models()
