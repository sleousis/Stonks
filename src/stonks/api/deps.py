"""Shared dependencies: the services container, auth and pagination."""

from __future__ import annotations

import hmac
import ipaddress
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from stonks.accounts import Scope
from stonks.app.services import Services
from stonks.config import ApiConfig

_bearer = HTTPBearer(auto_error=False, description="STONKS_API_TOKEN")

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def get_services(request: Request) -> Services:
    return request.app.state.services


def get_api_config(request: Request) -> ApiConfig:
    return request.app.state.services.context.settings.api


ServicesDep = Annotated[Services, Depends(get_services)]


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _check_token(cfg: ApiConfig, creds: HTTPAuthorizationCredentials | None) -> None:
    if cfg.token is None:
        # Fail closed: without a configured token nothing mutating is allowed.
        raise HTTPException(
            status_code=503, detail="API token not configured; set STONKS_API_TOKEN"
        )
    supplied = creds.credentials if creds is not None else ""
    if not hmac.compare_digest(
        supplied.encode("utf-8"), cfg.token.get_secret_value().encode("utf-8")
    ):
        raise HTTPException(
            status_code=401,
            detail="missing or invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def authorize(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    """Applied to every ``/api`` router except health.

    Keyed on the HTTP method so a new mutating route can't forget auth:
    anything other than GET/HEAD/OPTIONS always needs the bearer token;
    reads skip it only when ``open_reads_on_loopback`` is on and the peer
    is a loopback address.
    """
    cfg = get_api_config(request)
    if request.method in _SAFE_METHODS and cfg.open_reads_on_loopback:
        client = request.client.host if request.client else None
        if _is_loopback(client):
            return
    _check_token(cfg, creds)


def require_token(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    """Always demands the bearer token, whatever the method or peer: for
    routes whose whole point is to verify it (``GET /api/auth/check``)."""
    _check_token(get_api_config(request), creds)


def authorize_stream(
    request: Request,
    job_id: str,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    token: Annotated[
        str | None,
        Query(
            max_length=512,
            description="Stream token from POST /api/jobs/{job_id}/stream-token, for "
            "clients (browser EventSource) that cannot send the bearer header.",
        ),
    ] = None,
) -> None:
    """Auth for a job's event stream: a valid stream token for *this* job,
    or whatever :func:`authorize` accepts. A bad token is always 401."""
    if token is not None:
        if get_services(request).jobs.verify_stream_token(job_id, token):
            return
        raise HTTPException(status_code=401, detail="invalid or expired stream token")
    authorize(request, creds)


def current_scope(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Scope:
    """The authenticated principal's data scope, for user-scoped routes
    (connections, push, notifications, audited runs).

    Always demands the bearer token: loopback reads carry no principal, so
    personal data has no open-reads exemption. Until login and per-user
    tokens land (step S2, which swaps this one function), the token belongs
    to the bootstrap admin ``usr_owner``.
    """
    _check_token(get_api_config(request), creds)
    return get_services(request).bootstrap_scope()


ScopeDep = Annotated[Scope, Depends(current_scope)]


@dataclass(frozen=True)
class PageParams:
    limit: int
    offset: int


def page_params(
    request: Request,
    limit: Annotated[int | None, Query(ge=1, description="page size")] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PageParams:
    cfg = get_api_config(request)
    size = cfg.default_page_size if limit is None else limit
    if size > cfg.max_page_size:
        from stonks.app.errors import ValidationError

        raise ValidationError(f"limit must be <= {cfg.max_page_size}")
    return PageParams(limit=size, offset=offset)


PageDep = Annotated[PageParams, Depends(page_params)]
