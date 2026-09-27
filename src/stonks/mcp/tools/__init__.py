"""MCP tool modules, one per safety class. Each exposes
``register(t: ToolContext)``; :data:`MODULES` is the order they register in."""

from __future__ import annotations

from stonks.mcp.tools import (
    connections,
    factors,
    guarded,
    halts,
    insights,
    jobs,
    notifications,
    reads,
    risk,
    studio,
    subscriptions,
    tca,
    universes,
    workspace,
)
from stonks.mcp.tools.common import ToolContext

MODULES = (
    reads,
    jobs,
    guarded,
    studio,
    connections,
    halts,
    universes,
    tca,
    factors,
    subscriptions,
    insights,
    risk,
    notifications,
    workspace,
)


def register_all(t: ToolContext) -> None:
    for module in MODULES:
        module.register(t)
