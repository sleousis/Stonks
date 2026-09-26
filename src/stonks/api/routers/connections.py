"""Broker connections (design section 6): the caller's links to brokers and
aggregators, synced into broker-kind portfolios.

Every route takes the caller's scope (:func:`~stonks.api.deps.current_scope`),
so each needs the bearer token, reads included, and another user's
connection id is a 404. The service writes the audit rows. Credentials only
ever travel in the body of ``POST /api/connections/keys``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from stonks.api.deps import ScopeDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.connections import (
    BrokerAccountView,
    ConnectionView,
    ConnectWithKeysRequest,
    DisconnectView,
    LinkAccountRequest,
    LinkResultView,
    PortalLinkView,
    ProviderView,
    StartPortalRequest,
    SyncResultView,
)
from stonks.auth import Permission

router = APIRouter(prefix="/api/connections", tags=["connections"], responses=PROBLEM_RESPONSES)


@router.get("/providers", response_model=list[ProviderView], operation_id="listProviders")
def list_providers(services: ServicesDep, scope: ScopeDep) -> list[ProviderView]:
    """Providers an admin enabled (``[connections].enabled_providers``) and
    configured; nothing else can be connected."""
    return services.connections.providers(scope)


@router.get("", response_model=list[ConnectionView], operation_id="listConnections")
def list_connections(services: ServicesDep, scope: ScopeDep) -> list[ConnectionView]:
    """The caller's connections, oldest first."""
    return services.connections.list(scope)


@router.post(
    "/keys",
    status_code=201,
    response_model=ConnectionView,
    operation_id="connectWithKeys",
    dependencies=needs(Permission.CONNECTION_MANAGE),
)
def connect_with_keys(
    body: ConnectWithKeysRequest, services: ServicesDep, scope: ScopeDep
) -> ConnectionView:
    """Connect with API keys. The keys are checked against the provider
    first (refused keys are never stored), then sealed at rest; they are
    never returned or logged. New accounts get a broker portfolio each."""
    return services.connections.connect_with_keys(scope, body)


@router.post(
    "/portal",
    status_code=201,
    response_model=PortalLinkView,
    operation_id="startConnectionPortal",
    dependencies=needs(Permission.CONNECTION_MANAGE),
)
def start_portal(
    body: StartPortalRequest, services: ServicesDep, scope: ScopeDep
) -> PortalLinkView:
    """Start a hosted-login connection: returns the one-time provider URL.
    The provider redirects to ``redirect_uri`` with ``connection_id`` and
    ``state``; pass both to ``GET /api/connections/callback``."""
    return services.connections.start_portal(scope, body)


@router.get("/callback", response_model=ConnectionView, operation_id="completeConnectionPortal")
def complete_portal(
    services: ServicesDep,
    scope: ScopeDep,
    connection_id: Annotated[str, Query(min_length=1, max_length=64)],
    state: Annotated[str, Query(min_length=1, max_length=256)],
    status: Annotated[
        str | None, Query(max_length=64, description="the provider's outcome, if it sends one")
    ] = None,
) -> ConnectionView:
    """Finish a hosted-login connection: checks the one-time ``state`` and
    lists the accounts the user linked at the provider."""
    return services.connections.complete_portal(scope, connection_id, state, status)


@router.get("/{connection_id}", response_model=ConnectionView, operation_id="getConnection")
def get_connection(connection_id: str, services: ServicesDep, scope: ScopeDep) -> ConnectionView:
    return services.connections.get(scope, connection_id)


@router.delete(
    "/{connection_id}",
    response_model=DisconnectView,
    operation_id="deleteConnection",
    dependencies=needs(Permission.CONNECTION_MANAGE),
)
def delete_connection(connection_id: str, services: ServicesDep, scope: ScopeDep) -> DisconnectView:
    """Remove the connection, its credentials, accounts and activities.
    Linked portfolios are archived (their snapshots stay)."""
    return services.connections.disconnect(scope, connection_id)


@router.get(
    "/{connection_id}/accounts",
    response_model=list[BrokerAccountView],
    operation_id="listConnectionAccounts",
)
def list_accounts(
    connection_id: str, services: ServicesDep, scope: ScopeDep
) -> list[BrokerAccountView]:
    """External accounts seen on the connection, with the linked portfolio."""
    return services.connections.accounts(scope, connection_id)


@router.post(
    "/{connection_id}/link",
    response_model=LinkResultView,
    operation_id="linkConnectionAccount",
    dependencies=needs(Permission.PORTFOLIO_MANAGE),
)
def link_account(
    connection_id: str, body: LinkAccountRequest, services: ServicesDep, scope: ScopeDep
) -> LinkResultView:
    """Link an external account to one of your broker portfolios (a new one
    when ``portfolio_id`` is omitted)."""
    return services.connections.link_account(scope, connection_id, body)


@router.post(
    "/{connection_id}/sync",
    response_model=SyncResultView,
    operation_id="syncConnection",
    dependencies=needs(Permission.PORTFOLIO_MANAGE),
)
def sync_connection(connection_id: str, services: ServicesDep, scope: ScopeDep) -> SyncResultView:
    """Sync now (read-only at the provider). A provider failure is a 200
    with ``status = "error"``; the connection records it and backs off."""
    return services.connections.sync(scope, connection_id)
