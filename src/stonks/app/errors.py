"""Transport-neutral error types raised by the service layer.

The REST API maps each subclass onto an HTTP status and a problem-details
body; the CLI and MCP server map them onto their own conventions.
"""

from __future__ import annotations


class AppError(Exception):
    """Base for expected, user-facing service errors."""

    title = "Application error"


class NotFoundError(AppError):
    title = "Not found"


class ValidationError(AppError):
    title = "Invalid request"


class ConflictError(AppError):
    title = "Conflict"


class ConfigurationError(AppError):
    """A required setting (API key, token, ...) is missing."""

    title = "Service not configured"
