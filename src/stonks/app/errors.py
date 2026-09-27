"""Transport-neutral error types raised by the service layer.

The REST API maps each subclass onto an HTTP status and a problem-details
body; the CLI and MCP server map them onto their own conventions.
"""

from __future__ import annotations

from typing import ClassVar


class AppError(Exception):
    """Base for expected, user-facing service errors. ``code`` is a stable
    machine code clients may match on (problem details ``code``)."""

    title = "Application error"
    code: ClassVar[str] = "error"


class NotFoundError(AppError):
    title = "Not found"
    code = "not_found"


class ValidationError(AppError):
    title = "Invalid request"
    code = "invalid_request"


class ConflictError(AppError):
    title = "Conflict"
    code = "conflict"


class ConfigurationError(AppError):
    """A required setting (API key, token, ...) is missing."""

    title = "Service not configured"
    code = "not_configured"


class RateLimitedError(AppError):
    """The caller asked too often; ``retry_after`` seconds until it may try
    again (HTTP 429 with ``Retry-After``)."""

    title = "Too many requests"
    code = "rate_limited"
    http_status = 429

    def __init__(self, message: str, *, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.headers = {"Retry-After": str(retry_after)}
