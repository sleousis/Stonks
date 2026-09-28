from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from stonks.api.deps import (
    OptionalPrincipalDep,
    PageDep,
    PrincipalDep,
    ServicesDep,
    require_permission,
)
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.api.routers._jobs_common import JOB_CREATED, accepted
from stonks.app.errors import NotFoundError
from stonks.app.jobs import Job
from stonks.app.pagination import Page, page_of
from stonks.app.universes import (
    UNIVERSE_ENSURE_JOB,
    UNIVERSE_REFRESH_JOB,
    EnsureDataRequest,
    ExchangeView,
    IndexHistoryImport,
    IndexHistoryView,
    MembershipSpanView,
    UniverseCreate,
    UniverseMembers,
    UniverseRefreshView,
    UniverseUpdate,
    UniverseView,
)
from stonks.auth import Permission, require
from stonks.auth.principal import Principal
from stonks.ingest.ensure import EnsureReport

router = APIRouter(prefix="/api/universes", tags=["universes"], responses=PROBLEM_RESPONSES)

#: Creating, refreshing and filling universes is research work.
_lab = [Depends(require_permission(Permission.LAB_RUN))]
#: Deleting one can break the lab runs and ticks that name it: admins only.
_admin = [Depends(require_permission(Permission.STRATEGY_PROMOTE))]


def _trading_universe(services: ServicesDep) -> str | None:
    """The stored universe the production tick trades, if it names one."""
    universe = services.context.settings.production.universe
    return universe if isinstance(universe, str) else None


def _guard_trading(services: ServicesDep, principal: Principal, universe_id: str) -> None:
    """The trading universe decides what every book buys. Changing it is an
    admin setting, so changing its definition is admin work too."""
    if universe_id == _trading_universe(services):
        require(principal, Permission.STRATEGY_PROMOTE)


def _guard_trading_index(services: ServicesDep, principal: Principal, index_id: str) -> None:
    """An index universe follows the constituents imported for its index."""
    trading = _trading_universe(services)
    if trading is None:
        return
    try:
        definition = services.universes.get(trading)
    except NotFoundError:
        return
    if definition.kind == "index" and definition.spec.get("index_id") == index_id:
        require(principal, Permission.STRATEGY_PROMOTE)


@router.get("", response_model=Page[UniverseView], operation_id="listUniverses")
def list_universes(services: ServicesDep, page: PageDep) -> Page[UniverseView]:
    """Every stored universe, by id."""
    return page_of(services.universes.list(), page)


@router.post(
    "",
    response_model=UniverseView,
    status_code=201,
    operation_id="createUniverse",
    dependencies=_lab,
)
def create_universe(body: UniverseCreate, services: ServicesDep) -> UniverseView:
    """Store a universe definition (409 when the id exists). It has no
    members until its first refresh."""
    return services.universes.create(body)


@router.post(
    "/index-history",
    response_model=IndexHistoryView,
    operation_id="importIndexHistory",
    dependencies=_lab,
)
def import_index_history(
    body: IndexHistoryImport, services: ServicesDep, principal: PrincipalDep
) -> IndexHistoryView:
    """Import an index's constituents and changes (CSV or JSON) for
    ``index`` universes to rebuild membership from. The index the trading
    universe follows needs ``strategy.promote``."""
    _guard_trading_index(services, principal, body.index_id)
    return services.universes.import_index_history(body)


@router.get("/exchanges", response_model=Page[ExchangeView], operation_id="listUniverseExchanges")
def list_exchanges(services: ServicesDep, page: PageDep) -> Page[ExchangeView]:
    """Exchanges our instruments name, with counts, for picking an
    ``exchange`` universe. A source may list more than we hold."""
    return page_of(services.universes.exchanges(), page)


@router.get(
    "/refresh/{job_id}/result",
    response_model=UniverseRefreshView,
    operation_id="getUniverseRefreshResult",
)
def get_refresh_result(
    job_id: str, services: ServicesDep, principal: OptionalPrincipalDep
) -> UniverseRefreshView:
    """The result of a succeeded refresh job (409 until it has succeeded)."""
    return services.jobs.typed_result(job_id, UNIVERSE_REFRESH_JOB, UniverseRefreshView, principal)


@router.get(
    "/ensure/{job_id}/result", response_model=EnsureReport, operation_id="getUniverseEnsureResult"
)
def get_ensure_result(
    job_id: str, services: ServicesDep, principal: OptionalPrincipalDep
) -> EnsureReport:
    """The result of a succeeded ensure-data job (409 until it has succeeded)."""
    return services.jobs.typed_result(job_id, UNIVERSE_ENSURE_JOB, EnsureReport, principal)


@router.get("/{universe_id}", response_model=UniverseView, operation_id="getUniverse")
def get_universe(universe_id: str, services: ServicesDep) -> UniverseView:
    return services.universes.get(universe_id)


@router.put(
    "/{universe_id}",
    response_model=UniverseView,
    operation_id="updateUniverse",
    dependencies=_lab,
)
def update_universe(
    universe_id: str, body: UniverseUpdate, services: ServicesDep, principal: PrincipalDep
) -> UniverseView:
    """Replace the definition. The members stay as they are until the next
    refresh. The trading universe (``[production].universe``) needs
    ``strategy.promote``."""
    _guard_trading(services, principal, universe_id)
    return services.universes.update(universe_id, body)


@router.delete(
    "/{universe_id}",
    response_model=UniverseView,
    operation_id="deleteUniverse",
    dependencies=_admin,
)
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


@router.get(
    "/{universe_id}/history",
    response_model=Page[MembershipSpanView],
    operation_id="getUniverseHistory",
)
def get_history(
    universe_id: str,
    services: ServicesDep,
    page: PageDep,
    ticker: Annotated[
        str | None, Query(max_length=64, description="tickers containing this")
    ] = None,
) -> Page[MembershipSpanView]:
    """Membership spans, latest change first: who joined and left, when."""
    return services.universes.history(
        universe_id, ticker=ticker, limit=page.limit, offset=page.offset
    )


@router.post(
    "/{universe_id}/refresh", **JOB_CREATED, operation_id="refreshUniverse", dependencies=_lab
)
def refresh_universe(
    universe_id: str, services: ServicesDep, principal: PrincipalDep, response: Response
) -> Job:
    """Queue a refresh that rebuilds the universe's membership; poll
    ``/api/jobs/{id}`` or stream its events."""
    return accepted(
        services.universes.submit_refresh(universe_id, owner_id=principal.user_id), response
    )


@router.post(
    "/{universe_id}/ensure", **JOB_CREATED, operation_id="ensureUniverseData", dependencies=_lab
)
def ensure_data(
    universe_id: str,
    body: EnsureDataRequest,
    services: ServicesDep,
    principal: PrincipalDep,
    response: Response,
) -> Job:
    """Queue a job that fetches the missing bars of every member over the
    window, delisted names included."""
    return accepted(
        services.universes.submit_ensure(universe_id, body, owner_id=principal.user_id),
        response,
    )
