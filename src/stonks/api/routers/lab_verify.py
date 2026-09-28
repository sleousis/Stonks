"""Lab verify (roadmap 23.9): rerun lab results from their stored manifests,
the job the weekly ``lab_verify`` scheduler action starts."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Response

from stonks.api.deps import OptionalPrincipalDep, PrincipalDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.api.routers._jobs_common import JOB_CREATED, accepted
from stonks.app.jobs import Job
from stonks.app.lab_verify import VERIFY_JOB, VerifyRequest, VerifyResultView
from stonks.auth import Permission

router = APIRouter(tags=["lab"], responses=PROBLEM_RESPONSES)


@router.post(
    "/api/lab/verify",
    **JOB_CREATED,
    operation_id="startLabVerify",
    dependencies=needs(Permission.LAB_RUN),
)
def start_verify(
    services: ServicesDep,
    principal: PrincipalDep,
    response: Response,
    body: Annotated[VerifyRequest | None, Body()] = None,
) -> Job:
    """Queue a rerun of lab runs or strategies from their stored manifests
    (every strategy of ``[lab.verify].statuses`` when ``targets`` is empty).
    Fetch the result from ``GET /api/lab/verify/jobs/{job_id}/result``."""
    job = services.lab_verify.submit(body or VerifyRequest(), owner_id=principal.user_id)
    return accepted(job, response)


@router.get(
    "/api/lab/verify/jobs/{job_id}/result",
    response_model=VerifyResultView,
    operation_id="getLabVerifyResult",
)
def get_verify_result(
    job_id: str, services: ServicesDep, principal: OptionalPrincipalDep
) -> VerifyResultView:
    """The result of a succeeded verify job (409 until it has succeeded):
    per target, the stored and rerun score, whether it moved, and the
    tickers whose data changed."""
    return services.jobs.typed_result(job_id, VERIFY_JOB, VerifyResultView, principal)
