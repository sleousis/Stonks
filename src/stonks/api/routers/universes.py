from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Response

from stonks.api.deps import ServicesDep
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.api.routers._jobs_common import JOB_CREATED, accepted
from stonks.app.jobs import Job
from stonks.app.universes import (
    UNIVERSE_ENSURE_JOB,
    UNIVERSE_REFRESH_JOB,
    EnsureDataRequest,
    IndexHistoryImport,
    IndexHistoryView,
    UniverseCreate,
    UniverseMembers,
    UniverseRefreshView,
    UniverseView,
)
from stonks.ingest.ensure import EnsureReport

router = APIRouter(prefix="/api/universes", tags=["universes"], responses=PROBLEM_RESPONSES)


@router.get("", response_model=list[UniverseView], operation_id="listUniverses")
def list_universes(services: ServicesDep) -> list[UniverseView]:
    """Every stored universe, by id."""
    return services.universes.list()


@router.post("", response_model=UniverseView, status_code=201, operation_id="createUniverse")
def create_universe(body: UniverseCreate, services: ServicesDep) -> UniverseView:
    """Store a universe definition (409 when the id exists). It has no
    members until its first refresh."""
    return services.universes.create(body)


@router.post("/index-history", response_model=IndexHistoryView, operation_id="importIndexHistory")
def import_index_history(body: IndexHistoryImport, services: ServicesDep) -> IndexHistoryView:
    """Import an index's constituents and changes (CSV or JSON) for
    ``index`` universes to rebuild membership from."""
    return services.universes.import_index_history(body)


@router.get(
    "/refresh/{job_id}/result",
    response_model=UniverseRefreshView,
    operation_id="getUniverseRefreshResult",
)
def get_refresh_result(job_id: str, services: ServicesDep) -> UniverseRefreshView:
    """The result of a succeeded refresh job (409 until it has succeeded)."""
    return services.jobs.typed_result(job_id, UNIVERSE_REFRESH_JOB, UniverseRefreshView)


@router.get(
    "/ensure/{job_id}/result", response_model=EnsureReport, operation_id="getUniverseEnsureResult"
)
def get_ensure_result(job_id: str, services: ServicesDep) -> EnsureReport:
    """The result of a succeeded ensure-data job (409 until it has succeeded)."""
    return services.jobs.typed_result(job_id, UNIVERSE_ENSURE_JOB, EnsureReport)


@router.get("/{universe_id}", response_model=UniverseView, operation_id="getUniverse")
def get_universe(universe_id: str, services: ServicesDep) -> UniverseView:
    return services.universes.get(universe_id)


@router.delete("/{universe_id}", response_model=UniverseView, operation_id="deleteUniverse")
def delete_universe(universe_id: str, services: ServicesDep) -> UniverseView:
    """Delete the definition and its membership rows (returns the definition)."""
    return services.universes.delete(universe_id)


@router.get(
    "/{universe_id}/members", response_model=UniverseMembers, operation_id="getUniverseMembers"
)
def get_members(
    universe_id: str, services: ServicesDep, as_of: date | None = None
) -> UniverseMembers:
    """Members on ``as_of`` (default today), point in time."""
    return services.universes.members(universe_id, as_of)


@router.post("/{universe_id}/refresh", **JOB_CREATED, operation_id="refreshUniverse")
def refresh_universe(universe_id: str, services: ServicesDep, response: Response) -> Job:
    """Queue a refresh that rebuilds the universe's membership; poll
    ``/api/jobs/{id}`` or stream its events."""
    return accepted(services.universes.submit_refresh(universe_id), response)


@router.post("/{universe_id}/ensure", **JOB_CREATED, operation_id="ensureUniverseData")
def ensure_data(
    universe_id: str, body: EnsureDataRequest, services: ServicesDep, response: Response
) -> Job:
    """Queue a job that fetches the missing bars of every member over the
    window, delisted names included."""
    return accepted(services.universes.submit_ensure(universe_id, body), response)
