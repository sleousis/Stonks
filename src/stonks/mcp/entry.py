"""Process entrypoint for ``stonks mcp`` (stdio transport).

stdout carries the MCP protocol, so before anything logs, structlog is
pointed at stderr (its default factory prints to stdout).
"""

from __future__ import annotations

import sys

import structlog

from stonks.config import Settings
from stonks.logging import configure_logging, get_logger
from stonks.mcp.client import ApiClient


class McpConfigError(ValueError):
    """``[mcp]`` settings that must not be used (e.g. token over plain http)."""


def logs_to_stderr(level: str) -> None:
    configure_logging(level=level)
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(sys.stderr))


def api_client(settings: Settings) -> ApiClient:
    """Raises :class:`McpConfigError` for an unsafe api_url/token combination."""
    token = settings.api.token.get_secret_value() if settings.api.token else None
    try:
        return ApiClient(
            settings.mcp.api_url, token=token, timeout=settings.mcp.timeout_seconds
        )
    except ValueError as exc:
        raise McpConfigError(str(exc)) from None


def run(settings: Settings) -> None:
    logs_to_stderr(settings.logging.level)
    api = api_client(settings)
    get_logger("stonks.mcp").info(
        "mcp.started", api_url=api.base_url, write_tools_enabled=api.has_token
    )
    from stonks.mcp.server import run_stdio

    run_stdio(api, max_wait_seconds=settings.mcp.max_wait_seconds)
