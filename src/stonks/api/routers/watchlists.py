"""Your watchlists (roadmap 13.4): named ticker lists, never shared.
Another person's list is a 404, admins included."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Response

from stonks.api.deps import PageDep, PrincipalDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.pagination import Page, page_of
from stonks.app.watchlists import (
    WatchlistCreate,
    WatchlistService,
    WatchlistUpdate,
    WatchlistView,
)
from stonks.auth import Permission

router = APIRouter(prefix="/api/watchlists", tags=["watchlists"], responses=PROBLEM_RESPONSES)

WatchlistId = Annotated[str, Path(max_length=64)]


def _service(services: ServicesDep) -> WatchlistService:
    return WatchlistService(services.context)


@router.get("", response_model=Page[WatchlistView], operation_id="listWatchlists")
def list_watchlists(
    services: ServicesDep, principal: PrincipalDep, page: PageDep
) -> Page[WatchlistView]:
    """Your watchlists, oldest first."""
    return page_of(_service(services).list(principal), page)


@router.post(
    "",
    status_code=201,
    response_model=WatchlistView,
    operation_id="createWatchlist",
    dependencies=needs(Permission.PORTFOLIO_MANAGE),
)
def create_watchlist(
    body: WatchlistCreate, services: ServicesDep, principal: PrincipalDep
) -> WatchlistView:
    """Start a watchlist. Tickers are trimmed, upper-cased and kept once, in
    order. Names are unique per person (409)."""
    return _service(services).create(principal, body)


@router.get("/{watchlist_id}", response_model=WatchlistView, operation_id="getWatchlist")
def get_watchlist(
    watchlist_id: WatchlistId, services: ServicesDep, principal: PrincipalDep
) -> WatchlistView:
    return _service(services).get(principal, watchlist_id)


@router.patch(
    "/{watchlist_id}",
    response_model=WatchlistView,
    operation_id="updateWatchlist",
    dependencies=needs(Permission.PORTFOLIO_MANAGE),
)
def update_watchlist(
    watchlist_id: WatchlistId,
    body: WatchlistUpdate,
    services: ServicesDep,
    principal: PrincipalDep,
) -> WatchlistView:
    """Rename a watchlist, replace its tickers, or both."""
    return _service(services).update(principal, watchlist_id, body)


@router.delete(
    "/{watchlist_id}",
    status_code=204,
    response_class=Response,
    operation_id="deleteWatchlist",
    dependencies=needs(Permission.PORTFOLIO_MANAGE),
)
def delete_watchlist(
    watchlist_id: WatchlistId, services: ServicesDep, principal: PrincipalDep
) -> Response:
    _service(services).delete(principal, watchlist_id)
    return Response(status_code=204)
