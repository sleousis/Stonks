"""Model versions under one strategy id (roadmap 22.6): the versions and
their log, the swap check, a governed swap or reject, and the retrain job
the scheduler's ``model_retrain`` action starts."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Path, Response

from stonks.api.deps import OptionalPrincipalDep, PageDep, PrincipalDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.api.routers._jobs_common import JOB_CREATED, accepted
from stonks.app.jobs import Job
from stonks.app.model_versions import (
    RETRAIN_JOB,
    CalibrationView,
    ModelVersionView,
    RetrainRequest,
    RetrainResultView,
    SwapReportView,
    VersionChangeRequest,
    VersionEventView,
)
from stonks.app.pagination import Page, page_of
from stonks.auth import Permission

router = APIRouter(tags=["model-versions"], responses=PROBLEM_RESPONSES)

Version = Annotated[int, Path(ge=1, le=1_000_000)]
ChangeBody = Annotated[VersionChangeRequest | None, Body()]


@router.get(
    "/api/strategies/{strategy_id}/versions",
    response_model=Page[ModelVersionView],
    operation_id="listModelVersions",
)
def list_versions(strategy_id: str, services: ServicesDep, page: PageDep) -> Page[ModelVersionView]:
    """The strategy's model versions, oldest first: the live one, candidates
    running as model books, and archived, rejected or failed fits."""
    return page_of(services.model_versions.list(strategy_id), page)


@router.get(
    "/api/strategies/{strategy_id}/versions/history",
    response_model=Page[VersionEventView],
    operation_id="getModelVersionHistory",
)
def version_history(
    strategy_id: str, services: ServicesDep, page: PageDep
) -> Page[VersionEventView]:
    """The strategy's append-only version log, oldest first."""
    return page_of(services.model_versions.history(strategy_id), page)


@router.get(
    "/api/strategies/{strategy_id}/versions/{version}/check",
    response_model=SwapReportView,
    operation_id="checkModelSwap",
)
def check_swap(strategy_id: str, version: Version, services: ServicesDep) -> SwapReportView:
    """The swap check of a candidate against the live version
    (``[lifecycle.swap]``). Reports only."""
    return services.model_versions.check(strategy_id, version)


@router.get(
    "/api/strategies/{strategy_id}/versions/{version}/calibration",
    response_model=CalibrationView,
    operation_id="getModelCalibration",
)
def model_calibration(strategy_id: str, version: Version, services: ServicesDep) -> CalibrationView:
    """Live calibration of a classifier version's forecasts (roadmap 23.9):
    Brier score, base rate and reliability table."""
    return services.model_versions.calibration(strategy_id, version)


@router.post(
    "/api/strategies/{strategy_id}/versions/{version}/swap",
    response_model=ModelVersionView,
    operation_id="swapModelVersion",
    dependencies=needs(Permission.STRATEGY_PROMOTE),
)
def swap_version(
    strategy_id: str,
    version: Version,
    services: ServicesDep,
    principal: PrincipalDep,
    body: ChangeBody = None,
) -> ModelVersionView:
    """Make the candidate live: the next tick trades it. Needs a passing
    swap check (409 with the failing checks otherwise), or ``override``
    with a ``reason`` of at least 20 characters (422 when shorter)."""
    body = body or VersionChangeRequest()
    return services.model_versions.swap(
        strategy_id, version, actor=principal.actor, reason=body.reason, override=body.override
    )


@router.post(
    "/api/strategies/{strategy_id}/versions/{version}/reject",
    response_model=ModelVersionView,
    operation_id="rejectModelVersion",
    dependencies=needs(Permission.STRATEGY_PROMOTE),
)
def reject_version(
    strategy_id: str,
    version: Version,
    services: ServicesDep,
    principal: PrincipalDep,
    body: ChangeBody = None,
) -> ModelVersionView:
    """Drop a candidate. Needs a ``reason`` (422 without one)."""
    body = body or VersionChangeRequest()
    return services.model_versions.reject(
        strategy_id, version, actor=principal.actor, reason=body.reason
    )


@router.get(
    "/api/model-versions/candidates",
    response_model=Page[ModelVersionView],
    operation_id="listModelCandidates",
)
def list_candidates(services: ServicesDep, page: PageDep) -> Page[ModelVersionView]:
    """Every candidate version running as a model book, across strategies."""
    return page_of(services.model_versions.candidates(), page)


@router.post(
    "/api/model-versions/retrain",
    **JOB_CREATED,
    operation_id="startModelRetrain",
    dependencies=needs(Permission.LAB_RUN),
)
def start_retrain(
    services: ServicesDep,
    principal: PrincipalDep,
    response: Response,
    body: Annotated[RetrainRequest | None, Body()] = None,
) -> Job:
    """Queue a retrain of the strategies that learn from data. Each fit
    becomes a candidate version, and nothing trades until a swap. Fetch the
    result from ``GET /api/model-versions/jobs/{job_id}/result``."""
    job = services.model_versions.submit_retrain(
        body or RetrainRequest(), actor=principal.actor, owner_id=principal.user_id
    )
    return accepted(job, response)


@router.get(
    "/api/model-versions/jobs/{job_id}/result",
    response_model=RetrainResultView,
    operation_id="getModelRetrainResult",
)
def get_retrain_result(
    job_id: str, services: ServicesDep, principal: OptionalPrincipalDep
) -> RetrainResultView:
    """The result of a succeeded retrain job (409 until it has succeeded)."""
    return services.jobs.typed_result(job_id, RETRAIN_JOB, RetrainResultView, principal)
