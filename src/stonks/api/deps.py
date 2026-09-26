"""Shared dependencies: the services container, auth and pagination.

Every request resolves to one :class:`~stonks.auth.Principal` (session
cookie with CSRF, personal API token, or the legacy ``STONKS_API_TOKEN``
mapped to the bootstrap admin). See ``docs/security.md``."""

from __future__ import annotations

import hmac
import ipaddress
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from stonks.accounts import Scope
from stonks.app.services import Services
from stonks.auth import (
    AuthService,
    NotAuthenticated,
    Permission,
    PermissionDenied,
    Principal,
    SessionInfo,
    require,
)
from stonks.config import ApiConfig

_bearer = HTTPBearer(
    auto_error=False,
    description="A personal API token (stk_...) or, while it lasts, STONKS_API_TOKEN",
)

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


SESSION_COOKIE = "stonks_session"
CSRF_COOKIE = "stonks_csrf"
CSRF_HEADER = "X-CSRF-Token"


def get_auth(request: Request) -> AuthService:
    """The app's :class:`AuthService` (``Services.auth``). Tests may set
    ``app.state.auth`` to use another one."""
    override = getattr(request.app.state, "auth", None)
    return override if override is not None else get_services(request).auth


AuthDep = Annotated[AuthService, Depends(get_auth)]


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _resolve(request: Request, creds: HTTPAuthorizationCredentials | None) -> Principal:
    """One principal per request, cached on ``request.state``.

    A bearer token wins (an API token ``stk_...`` or the legacy
    ``STONKS_API_TOKEN``); otherwise the session cookie, which also needs
    the ``X-CSRF-Token`` header on unsafe methods. A session still waiting
    for its second factor is refused (401 ``mfa_required``)."""
    cached = getattr(request.state, "principal", None)
    if cached is not None:
        return cached
    auth = get_auth(request)
    if creds is not None:
        principal = auth.principal_for_bearer(creds.credentials)
    elif request.cookies.get(SESSION_COOKIE):
        principal = auth.principal_for_session(
            request.cookies.get(SESSION_COOKIE),
            csrf=request.headers.get(CSRF_HEADER),
            unsafe=request.method not in _SAFE_METHODS,
        )
    else:
        raise NotAuthenticated()
    request.state.principal = principal
    return principal


def authorize(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    """Applied to every ``/api`` router except health.

    Keyed on the HTTP method so a new mutating route can't forget auth:
    anything other than GET/HEAD/OPTIONS always needs a principal whose
    credential may write (scope ``trade``, ``lab`` or ``admin``); reads skip
    auth only when ``open_reads_on_loopback`` is on and the peer is a
    loopback address.
    """
    cfg = get_api_config(request)
    safe = request.method in _SAFE_METHODS
    if safe and cfg.open_reads_on_loopback and _is_loopback(_client_ip(request)):
        return
    principal = _resolve(request, creds)
    if not safe and not principal.can_write:
        raise PermissionDenied("this credential is read-only")


def require_token(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    """Always demands a principal, whatever the method or peer: for routes
    about the caller (``/api/auth/*``)."""
    _resolve(request, creds)


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
    """Auth for a job's event stream: a valid stream token for *this* job
    whose user is still active, or whatever :func:`authorize` accepts. A bad
    token is always 401."""
    if token is not None:
        user_id = get_services(request).jobs.verify_stream_token(job_id, token)
        if user_id is not None and get_auth(request).is_active_user(user_id):
            return
        raise HTTPException(status_code=401, detail="invalid or expired stream token")
    authorize(request, creds)


def current_principal(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    """The authenticated principal. Always demands a credential: loopback
    reads carry no principal, so personal data has no open-reads exemption."""
    return _resolve(request, creds)


PrincipalDep = Annotated[Principal, Depends(current_principal)]


def optional_principal(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal | None:
    """The caller, or ``None`` for a credential-less loopback read that
    :func:`authorize` let through (``open_reads_on_loopback``, dev only).
    Routes over "global, attributed" rows (jobs, drafts) scope by it."""
    if creds is None and not request.cookies.get(SESSION_COOKIE):
        return getattr(request.state, "principal", None)
    return _resolve(request, creds)


OptionalPrincipalDep = Annotated[Principal | None, Depends(optional_principal)]


def current_scope(principal: PrincipalDep) -> Scope:
    """The principal's data scope, for user-scoped routes (connections,
    push, notifications, audited runs)."""
    return principal.scope


ScopeDep = Annotated[Scope, Depends(current_scope)]


def owned_portfolio_id(
    services: ServicesDep,
    principal: PrincipalDep,
    portfolio_id: Annotated[
        str | None,
        Query(
            max_length=64,
            description="One of your portfolios (404 otherwise). Default: your own book.",
        ),
    ] = None,
) -> str:
    """The portfolio a read is about, checked against the caller: another
    user's id is a 404 (admins included; they get totals instead)."""
    return services.portfolio.resolve(principal, portfolio_id)


PortfolioIdDep = Annotated[str, Depends(owned_portfolio_id)]


_PERMISSION_ATTR = "__stonks_permission__"


def require_permission(permission: Permission) -> Callable[[Principal], None]:
    """Route dependency: ``Depends(require_permission(Permission.X))``.

    Every unsafe route declares one (a test walks the route table). The
    OpenAPI spec lists it per operation as ``x-permission``."""

    def dependency(principal: PrincipalDep) -> None:
        require(principal, permission)

    dependency.__name__ = f"require_{permission.name.lower()}"
    setattr(dependency, _PERMISSION_ATTR, permission)
    return dependency


def needs(permission: Permission) -> list[Any]:
    """``dependencies=needs(Permission.X)`` on a route decorator."""
    return [Depends(require_permission(permission))]


def permission_of(call: object) -> Permission | None:
    """The permission a :func:`require_permission` dependency checks."""
    return getattr(call, _PERMISSION_ATTR, None)


def route_permissions(routes: Iterable[Any]) -> list[tuple[str, str, Permission]]:
    """``(method, path, permission)`` for every route that declares one,
    through included routers."""
    from fastapi.routing import APIRoute

    def found(dependant: Any) -> Permission | None:
        for dep in dependant.dependencies:
            perm = permission_of(dep.call) or found(dep)
            if perm is not None:
                return perm
        return None

    out: list[tuple[str, str, Permission]] = []
    for route in routes:
        if isinstance(route, APIRoute):
            perm = found(route.dependant)
            if perm is not None:
                out.extend((m, route.path, perm) for m in sorted(route.methods))
        elif hasattr(route, "original_router"):
            out.extend(route_permissions(route.original_router.routes))
    return out


def current_session(request: Request) -> SessionInfo:
    """The browser session behind the cookie, pending or full (for the
    second-factor routes). Unsafe methods need the CSRF header."""
    return get_auth(request).session(
        request.cookies.get(SESSION_COOKIE),
        csrf=request.headers.get(CSRF_HEADER),
        unsafe=request.method not in _SAFE_METHODS,
    )


SessionDep = Annotated[SessionInfo, Depends(current_session)]


class MetricsAccessConfig(BaseSettings):
    """Who may scrape ``GET /metrics`` (env-only, like the API token):

    - ``STONKS_METRICS_TOKEN``: a scrape-only bearer token. The API token is
      deliberately not accepted, so Prometheus never holds an admin credential.
    - ``STONKS_METRICS_ALLOW_LOOPBACK`` (default true): scrapes from a
      loopback peer need no token.
    """

    model_config = SettingsConfigDict(env_prefix="STONKS_METRICS_", extra="ignore")

    token: SecretStr | None = None
    allow_loopback: bool = True


def authorize_metrics(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    cfg: MetricsAccessConfig = request.app.state.metrics_access
    if creds is None:
        client = request.client.host if request.client else None
        if cfg.allow_loopback and _is_loopback(client):
            return
    elif cfg.token is not None and hmac.compare_digest(
        creds.credentials.encode("utf-8"), cfg.token.get_secret_value().encode("utf-8")
    ):
        return
    raise HTTPException(
        status_code=401,
        detail="metrics need the scrape token (STONKS_METRICS_TOKEN) or a loopback peer",
        headers={"WWW-Authenticate": "Bearer"},
    )


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
