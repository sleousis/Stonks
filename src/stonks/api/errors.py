"""Problem-details (RFC 9457) error responses for every failure path."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from stonks.app.errors import (
    AppError,
    ConfigurationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from stonks.app.strategies import FailingCheck
from stonks.ingest.redact import redact_secrets
from stonks.logging import get_logger

PROBLEM_MEDIA_TYPE = "application/problem+json"

_log = get_logger("stonks.api.errors")


class ProblemDetails(BaseModel):
    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
    #: Stable machine code (``not_found``, ``step_up_required``,
    #: ``mfa_required``, ``auto_blocked``, ...). ``detail`` is for people.
    code: str | None = None
    #: 401 ``mfa_required``: ``enrol`` or ``verify``.
    next_step: str | None = None
    #: 409 ``auto_blocked``: every auto checklist item that fails.
    blockers: list[str] | None = None
    #: Field-level errors for 422 responses.
    errors: list[dict[str, Any]] | None = None
    #: A refused promotion (409): the go-live checks that failed.
    failing_checks: list[FailingCheck] | None = None


_STATUS_BY_ERROR: tuple[tuple[type[AppError], int], ...] = (
    (NotFoundError, 404),
    (ValidationError, 422),
    (ConflictError, 409),
    (ConfigurationError, 503),
)

#: Merge into a route's ``responses=`` so generated clients know the shape.
PROBLEM_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": ProblemDetails, "description": HTTPStatus(code).phrase}
    for code in (401, 404, 409, 422, 503)
}


#: Machine codes for errors that carry none of their own.
_CODE_BY_STATUS: dict[int, str] = {
    400: "bad_request",
    401: "not_authenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "too_large",
    422: "validation_failed",
    429: "too_many_attempts",
    500: "internal_error",
    503: "not_configured",
}


def problem(
    request: Request,
    status: int,
    *,
    title: str | None = None,
    detail: str | None = None,
    code: str | None = None,
    errors: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
    extensions: dict[str, Any] | None = None,
) -> JSONResponse:
    body = ProblemDetails(
        title=title or HTTPStatus(status).phrase,
        status=status,
        detail=detail,
        instance=request.url.path,
        errors=errors,
        code=code or _CODE_BY_STATUS.get(status, "error"),
        **(extensions or {}),
    )
    return JSONResponse(
        body.model_dump(exclude_none=True),
        status_code=status,
        media_type=PROBLEM_MEDIA_TYPE,
        headers=headers,
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        # Errors may carry their own status and headers (auth: 401/403/429).
        status = next(
            (code for cls, code in _STATUS_BY_ERROR if isinstance(exc, cls)),
            getattr(exc, "http_status", 400),
        )
        headers = getattr(exc, "headers", None)
        extend = getattr(exc, "problem_extensions", None)
        return problem(
            request,
            status,
            title=exc.title,
            detail=str(exc),
            code=getattr(exc, "code", None),
            headers=headers,
            extensions=extend() if callable(extend) else None,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else None
        return problem(request, exc.status_code, detail=detail, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"loc": list(e.get("loc", ())), "msg": str(e.get("msg", "")), "type": e.get("type")}
            for e in exc.errors()
        ]
        return problem(
            request, 422, title="Invalid request", detail="validation failed", errors=errors
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Never echo internals (paths, SQL, credentials) to the client, and
        # scrub the configured credentials from the log line too.
        _log.error(
            "api.unhandled_error",
            path=request.url.path,
            error=redact_secrets(str(exc), _configured_secrets(request)),
            error_type=type(exc).__name__,
        )
        return problem(request, 500, detail="internal server error")


def _configured_secrets(request: Request) -> list[str]:
    services = getattr(request.app.state, "services", None)
    if services is None:
        return []
    try:
        return list(services.runner.secrets())
    except Exception:  # a broken secrets hook must not mask the original error
        return []
