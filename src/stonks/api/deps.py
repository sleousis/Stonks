"""Shared dependencies: the services container, auth and pagination."""

from __future__ import annotations

import hmac
import ipaddress
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

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
