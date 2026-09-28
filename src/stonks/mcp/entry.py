"""Process entrypoint for ``stonks mcp`` (stdio transport).

stdout carries the MCP protocol, so before anything logs, structlog is
pointed at stderr (its default factory prints to stdout).

The server acts as one user: the owner of its token. ``STONKS_MCP_TOKEN``
holds that personal token (``stk_...``, with the scopes chosen when it was
made). ``STONKS_API_TOKEN``, the server's old shared token, is the
fallback and acts as the bootstrap admin.
"""

from __future__ import annotations

import os
import sys

import structlog

from stonks.config import Settings
from stonks.logging import configure_logging, get_logger
from stonks.mcp.client import ApiClient

#: A personal API token (``stk_<id>_<secret>``) names one user.
_PERSONAL_PREFIX = "stk_"


class McpConfigError(ValueError):
    """``[mcp]`` settings that must not be used (e.g. token over plain http)."""


def logs_to_stderr(level: str) -> None:
    configure_logging(level=level)
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(sys.stderr))


def mcp_token(settings: Settings) -> tuple[str | None, bool]:
    """The token ``stonks mcp`` sends, and whether it is the old shared one.

    ``STONKS_MCP_TOKEN`` wins over ``STONKS_API_TOKEN``, so the server's
    shared token in the same ``.env`` is never picked up by accident."""
    token = os.environ.get("STONKS_MCP_TOKEN") or None
    if token is None and settings.api.token is not None:
        token = settings.api.token.get_secret_value() or None
    shared = token is not None and not token.startswith(_PERSONAL_PREFIX)
    return token, shared


def api_client(settings: Settings) -> ApiClient:
    """Raises :class:`McpConfigError` for an unsafe api_url/token combination."""
    token, _ = mcp_token(settings)
    try:
        return ApiClient(settings.mcp.api_url, token=token, timeout=settings.mcp.timeout_seconds)
    except ValueError as exc:
        raise McpConfigError(str(exc)) from None


def resolve_toolsets(api: ApiClient) -> frozenset[str] | None:
    """The MCP tool groups the token may use, from ``GET /api/auth/me``
    (roadmap 23.8). None: every group. When the API cannot say (down, or
    the token is refused), only ``whoami`` stays: a limited token never
    gets more tools by accident. Closes ``api`` (a client of its own)."""
    import anyio

    async def ask() -> object:
        try:
            return await api.get("/api/auth/me")
        finally:
            await api.aclose()

    try:
        me = anyio.run(ask)
    except Exception as exc:
        get_logger("stonks.mcp").warning("mcp.toolsets_unknown", error=type(exc).__name__)
        return frozenset()
    groups = me.get("toolsets") if isinstance(me, dict) else None
    return None if groups is None else frozenset(str(g) for g in groups)


def run(settings: Settings) -> None:
    # Validate first: a bad config exits without reconfiguring logging.
    api = api_client(settings)
    logs_to_stderr(settings.logging.level)
    log = get_logger("stonks.mcp")
    if mcp_token(settings)[1]:
        log.warning(
            "mcp.shared_token",
            hint="the shared STONKS_API_TOKEN acts as the bootstrap admin; set "
            "STONKS_MCP_TOKEN to a personal token so every tool acts as you",
        )
    toolsets = resolve_toolsets(api_client(settings))
    log.info(
        "mcp.started",
        api_url=api.base_url,
        write_tools_enabled=api.has_token,
        toolsets=sorted(toolsets) if toolsets is not None else "all",
    )
    from stonks.mcp.server import run_stdio

    run_stdio(api, max_wait_seconds=settings.mcp.max_wait_seconds, toolsets=toolsets)
