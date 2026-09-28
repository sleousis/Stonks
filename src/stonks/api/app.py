"""FastAPI app factory for the Stonks REST API.

Only this package imports FastAPI/Starlette; routes are thin adapters over
:mod:`stonks.app` services. Route functions that touch the lake or state
are plain ``def`` so FastAPI runs them in its threadpool instead of
blocking the event loop.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from stonks.api.deps import (
    CSRF_HEADER,
    MetricsAccessConfig,
    authorize,
    authorize_metrics,
    authorize_stream,
    require_token,
    route_permissions,
)
from stonks.api.errors import PROBLEM_MEDIA_TYPE, install_error_handlers
from stonks.api.routers import (
    API_ROUTERS,
    METRICS_ROUTERS,
    PUBLIC_ROUTERS,
    STREAM_ROUTERS,
    TOKEN_ROUTERS,
)
from stonks.api.routers.health import _version
from stonks.api.routers.jobs import JobEvent
from stonks.api.static import mount_spa
from stonks.app.context import AppContext, SourceFactory
from stonks.app.services import Services
from stonks.config import Settings
from stonks.logging import get_logger

_log = get_logger("stonks.api")

_LOOPBACK_HOSTS = ["localhost", "127.0.0.1", "::1", "[::1]"]
#: Bind addresses that are not a host name a client would send.
_WILDCARD_BINDS = frozenset({"0.0.0.0", "::", "[::]", ""})


def create_app(
    settings: Settings,
    *,
    services: Services | None = None,
    source_factory: SourceFactory | None = None,
    sse_poll_seconds: float = 0.5,
) -> FastAPI:
    svc = services or Services.create(AppContext(settings, source_factory=source_factory))
    cfg = settings.api

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        svc.start()
        # Hosts the scheduler loop when [scheduler].backend resolves to
        # in_process; a no-op otherwise.
        svc.schedule.start_hosted()
        # The Telegram bot polls when [telegram].enabled and the token is set.
        svc.telegram.start_hosted()
        _log.info("api.started", host=cfg.host, port=cfg.port)
        try:
            yield
        finally:
            # Stop the loop first (it waits for a running job), so no new
            # job is submitted to a runner that is shutting down.
            svc.schedule.stop_hosted()
            svc.telegram.stop_hosted()
            # Queued jobs are cancelled and running lab runs asked to stop at
            # their next trial. wait=False only returns early: the
            # interpreter still joins running workers (ticks, ingests) at
            # exit. Anything cut off by a hard kill is marked failed by
            # recover_interrupted() on the next start.
            svc.shutdown(wait=False)
            _log.info("api.stopped")

    app = FastAPI(
        title="Stonks API",
        version=_version(),
        description="Research + trading system: portfolio, strategies, market data, "
        "lab runs and background jobs.",
        lifespan=lifespan,
    )
    app.state.services = svc
    svc.asgi_app = app
    app.state.sse_poll_seconds = sse_poll_seconds
    app.state.metrics_access = MetricsAccessConfig()

    install_error_handlers(app)
    for router in PUBLIC_ROUTERS:
        app.include_router(router)
    for router in API_ROUTERS:
        app.include_router(router, dependencies=[Depends(authorize)])
    for router in TOKEN_ROUTERS:
        app.include_router(router, dependencies=[Depends(require_token)])
    for router in STREAM_ROUTERS:
        app.include_router(router, dependencies=[Depends(authorize_stream)])
    for router in METRICS_ROUTERS:
        app.include_router(router, dependencies=[Depends(authorize_metrics)])
    if cfg.ui_dist.is_dir():
        mount_spa(app, cfg.ui_dist)
    _install_openapi_postprocessing(app)

    # Middleware: last added runs first. Host check first (DNS-rebinding
    # guard for the open-on-loopback reads), then CORS, then logging.
    app.add_middleware(_RequestLogMiddleware)
    # Only the Angular dev server's origin ([api].ui_origin) may call the
    # API cross-origin, with the session cookie and its CSRF header. The
    # built console is same-origin and needs no CORS at all.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[cfg.ui_origin],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Last-Event-ID", CSRF_HEADER],
    )
    allowed = [*_LOOPBACK_HOSTS, *cfg.allowed_hosts]
    if cfg.host not in _WILDCARD_BINDS:  # "0.0.0.0" binds every address, names none
        allowed.append(cfg.host)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=sorted(set(allowed)))
    return app


def _install_openapi_postprocessing(app: FastAPI) -> None:
    """Error responses are ``application/problem+json`` whatever the route's
    success media type, and the SSE route's ``data`` payload is typed as
    :class:`JobEvent` so generated clients get a model for it."""
    base_openapi = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        spec = base_openapi()
        problem_ref = {"$ref": "#/components/schemas/ProblemDetails"}
        schemas = spec.setdefault("components", {}).setdefault("schemas", {})
        schemas["JobEvent"] = JobEvent.model_json_schema(
            ref_template="#/components/schemas/{model}"
        )
        for item in spec.get("paths", {}).values():
            for op in item.values():
                for code, resp in op.get("responses", {}).items():
                    content = resp.get("content", {})
                    if code.isdigit() and int(code) >= 400 and content:
                        resp["content"] = {PROBLEM_MEDIA_TYPE: {"schema": problem_ref}}
                    stream = content.get("text/event-stream")
                    # Streams typed by their route (the assistant's events)
                    # keep their own schema. The job stream is typed here.
                    data = (stream or {}).get("itemSchema", {}).get("properties", {}).get("data")
                    if stream and "itemSchema" in stream and "contentSchema" not in (data or {}):
                        stream["itemSchema"]["properties"]["data"] = {
                            "contentMediaType": "application/json",
                            "contentSchema": {"$ref": "#/components/schemas/JobEvent"},
                        }
        # The permission each route checks (design section 8), for clients
        # and reviewers: ``x-permission: strategy.promote``.
        for method, path, permission in route_permissions(app.routes):
            op = spec.get("paths", {}).get(path, {}).get(method.lower())
            if op is not None:
                op["x-permission"] = permission.value
        app.openapi_schema = spec
        return spec

    app.openapi = openapi  # type: ignore[method-assign]


class _RequestLogMiddleware:
    """One structured log line per request. Pure ASGI (not
    ``BaseHTTPMiddleware``) so streamed SSE responses pass through
    untouched. Logs the path only — never headers or query strings, which
    may carry credentials."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid.uuid4().hex[:12]
        started = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            _log.info(
                "api.request",
                request_id=request_id,
                method=scope.get("method"),
                path=scope.get("path"),
                status=status,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            )
