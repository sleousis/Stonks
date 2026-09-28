"""Factor routes (roadmap 22.2, 22.3, 22.8): the factor library, formula
checks, values at a date, and factor tear sheet jobs."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query, Response

from stonks.api.deps import OptionalPrincipalDep, PrincipalDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.api.routers._jobs_common import JOB_CREATED, accepted
from stonks.app.factors import (
    FACTOR_TEARSHEET_JOB,
    ExpressionCheckRequest,
    ExpressionCheckView,
    FactorCatalogView,
    FactorTearSheetRequest,
    FactorTearSheetView,
    FactorValuesRequest,
    FactorValuesView,
    FactorView,
)
from stonks.app.jobs import Job
from stonks.auth import Permission

router = APIRouter(prefix="/api/factors", tags=["factors"], responses=PROBLEM_RESPONSES)

_Name = Annotated[str | None, Query(min_length=1, max_length=64)]


@router.get("", response_model=FactorCatalogView, operation_id="listFactors")
def list_factors(
    services: ServicesDep,
    family: _Name = None,
    set: _Name = None,
    kind: Literal["expression", "fundamental"] | None = None,
) -> FactorCatalogView:
    """The factor library (Alpha158, classic price factors, fundamentals
    scores), optionally one family, set or kind, with the sets and families."""
    return services.factors.catalog(family=family, set=set, kind=kind)


@router.post(
    "/check",
    response_model=ExpressionCheckView,
    operation_id="checkFactorExpression",
    dependencies=needs(Permission.READ),
)
def check_expression(body: ExpressionCheckRequest, services: ServicesDep) -> ExpressionCheckView:
    """Whether a formula parses and is point in time, its canonical form and
    its warm-up in bars. A bad formula is ``ok: false`` with the reason."""
    return services.factors.check_expression(body)


@router.post(
    "/values",
    response_model=FactorValuesView,
    operation_id="getFactorValues",
    dependencies=needs(Permission.READ),
)
def factor_values(body: FactorValuesRequest, services: ServicesDep) -> FactorValuesView:
    """Each universe name's factor value known at the close of ``as_of``,
    best first in the factor's direction."""
    return services.factors.values(body)


@router.post(
    "/tearsheets",
    **JOB_CREATED,
    operation_id="startFactorTearsheet",
    dependencies=needs(Permission.LAB_RUN),
)
def start_tearsheet(
    body: FactorTearSheetRequest,
    services: ServicesDep,
    principal: PrincipalDep,
    response: Response,
) -> Job:
    """Queue a factor tear sheet: IC per horizon and by sector, asset class
    and size, returns per quantile, factor alpha and beta, a monthly IC
    heatmap and turnover. Fetch it from ``GET
    /api/factors/tearsheets/{job_id}/result``. Universes under 10 tickers
    come back ``n/a``."""
    return accepted(services.factors.submit_tearsheet(body, owner_id=principal.user_id), response)


@router.get(
    "/tearsheets/{job_id}/result",
    response_model=FactorTearSheetView,
    operation_id="getFactorTearsheetResult",
)
def get_tearsheet_result(
    job_id: str, services: ServicesDep, principal: OptionalPrincipalDep
) -> FactorTearSheetView:
    """The result of a succeeded factor tear sheet job (409 until it has
    succeeded)."""
    return services.jobs.typed_result(job_id, FACTOR_TEARSHEET_JOB, FactorTearSheetView, principal)


@router.get("/{factor_id}", response_model=FactorView, operation_id="getFactor")
def get_factor(factor_id: str, services: ServicesDep) -> FactorView:
    """One library factor: formula, family, direction, hypothesis, warm-up."""
    return services.factors.get(factor_id)
