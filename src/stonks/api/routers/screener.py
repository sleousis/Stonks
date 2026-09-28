"""The screener (roadmap 20.8): run a screen on price and fundamental
metrics, keep your own saved screens, and store a screen as a universe for
the lab. Another person's screen is a 404, admins included."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Response

from stonks.api.deps import PageDep, PrincipalDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.api.routers._jobs_common import JOB_CREATED, accepted
from stonks.app.jobs import Job
from stonks.app.pagination import Page, page_of
from stonks.app.screener import (
    SCREEN_RUN_JOB,
    MetricView,
    SavedScreenCreate,
    SavedScreenUpdate,
    SavedScreenView,
    ScreenRunRequest,
    ScreenSize,
    ScreenUniverseRequest,
    ScreenUniverseView,
)
from stonks.auth import Permission
from stonks.screener import ScreenResult

router = APIRouter(prefix="/api/screener", tags=["screener"], responses=PROBLEM_RESPONSES)

ScreenId = Annotated[str, Path(max_length=64)]
JobId = Annotated[str, Path(max_length=64)]


@router.get("/metrics", response_model=list[MetricView], operation_id="listScreenMetrics")
def list_metrics(services: ServicesDep) -> list[MetricView]:
    """Every metric a screen may filter or sort on, with its unit."""
    return services.screener.metrics()


@router.post(
    "/run",
    response_model=ScreenResult,
    operation_id="runScreen",
    dependencies=needs(Permission.READ),
)
def run_screen(
    body: ScreenRunRequest, services: ServicesDep, principal: PrincipalDep
) -> ScreenResult:
    """Run a screen (or one of your saved ones) on the lake as it was on
    ``as_of`` (default today). Only data known on that day counts."""
    return services.screener.run(principal, body)


@router.post(
    "/size",
    response_model=ScreenSize,
    operation_id="sizeScreen",
    dependencies=needs(Permission.READ),
)
def size_screen(
    body: ScreenRunRequest, services: ServicesDep, principal: PrincipalDep
) -> ScreenSize:
    """Count a screen's candidates before running it (no metric is read):
    over the cap it would fail, above the job threshold run it as a job."""
    return services.screener.size(principal, body)


@router.post(
    "/jobs", **JOB_CREATED, operation_id="submitScreenJob", dependencies=needs(Permission.READ)
)
def submit_screen_job(
    body: ScreenRunRequest, services: ServicesDep, principal: PrincipalDep, response: Response
) -> Job:
    """Run a large screen as a background job; follow ``/api/jobs/{id}``
    or its event stream, then read ``/api/screener/jobs/{id}/result``."""
    return accepted(services.screener.submit(principal, body), response)


@router.get(
    "/jobs/{job_id}/result",
    response_model=ScreenResult,
    operation_id="getScreenJobResult",
    dependencies=needs(Permission.READ),
)
def get_screen_job_result(
    job_id: JobId, services: ServicesDep, principal: PrincipalDep
) -> ScreenResult:
    """The rows of a finished screen job (409 until it has succeeded)."""
    return services.jobs.typed_result(job_id, SCREEN_RUN_JOB, ScreenResult, principal)


@router.get("/screens", response_model=Page[SavedScreenView], operation_id="listScreens")
def list_screens(
    services: ServicesDep, principal: PrincipalDep, page: PageDep
) -> Page[SavedScreenView]:
    """Your saved screens, oldest first."""
    return page_of(services.screener.list(principal), page)


@router.post(
    "/screens",
    status_code=201,
    response_model=SavedScreenView,
    operation_id="createScreen",
    dependencies=needs(Permission.PORTFOLIO_MANAGE),
)
def create_screen(
    body: SavedScreenCreate, services: ServicesDep, principal: PrincipalDep
) -> SavedScreenView:
    """Save a screen. Names are unique per person (409)."""
    return services.screener.create(principal, body)


@router.get("/screens/{screen_id}", response_model=SavedScreenView, operation_id="getScreen")
def get_screen(
    screen_id: ScreenId, services: ServicesDep, principal: PrincipalDep
) -> SavedScreenView:
    return services.screener.get(principal, screen_id)


@router.patch(
    "/screens/{screen_id}",
    response_model=SavedScreenView,
    operation_id="updateScreen",
    dependencies=needs(Permission.PORTFOLIO_MANAGE),
)
def update_screen(
    screen_id: ScreenId,
    body: SavedScreenUpdate,
    services: ServicesDep,
    principal: PrincipalDep,
) -> SavedScreenView:
    """Rename a saved screen, replace its spec, or both."""
    return services.screener.update(principal, screen_id, body)


@router.delete(
    "/screens/{screen_id}",
    status_code=204,
    response_class=Response,
    operation_id="deleteScreen",
    dependencies=needs(Permission.PORTFOLIO_MANAGE),
)
def delete_screen(screen_id: ScreenId, services: ServicesDep, principal: PrincipalDep) -> Response:
    services.screener.delete(principal, screen_id)
    return Response(status_code=204)


@router.post(
    "/universes",
    status_code=201,
    response_model=ScreenUniverseView,
    operation_id="saveScreenAsUniverse",
    dependencies=needs(Permission.LAB_RUN),
)
def save_as_universe(
    body: ScreenUniverseRequest, services: ServicesDep, principal: PrincipalDep
) -> ScreenUniverseView:
    """Store a screen as a universe (409 when the id exists) and queue its
    refresh. Rule mode reruns the screen at each rebalance date, so the
    lab sees who passed on each day. Snapshot mode stores today's matches."""
    return services.screener.to_universe(principal, body)
