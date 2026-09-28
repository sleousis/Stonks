"""MCP tool modules, one per safety class. Each exposes
``register(t: ToolContext)``; :data:`MODULES` is the order they register in."""

from __future__ import annotations

from stonks.mcp.tools import (
    calendars,
    cash_flows,
    connections,
    decisions,
    factors,
    guarded,
    halts,
    insights,
    jobs,
    journal,
    live,
    model_versions,
    notifications,
    options,
    orders,
    price_alerts,
    reads,
    research,
    risk,
    screen_alerts,
    screener,
    studio,
    subscriptions,
    tax,
    tca,
    universes,
    workspace,
)
from stonks.mcp.tools.common import ToolContext

MODULES = (
    reads,
    jobs,
    guarded,
    model_versions,
    studio,
    connections,
    halts,
    orders,
    price_alerts,
    universes,
    tca,
    journal,
    tax,
    cash_flows,
    factors,
    subscriptions,
    insights,
    risk,
    decisions,
    notifications,
    workspace,
    research,
    calendars,
    screener,
    screen_alerts,
    live,
    options,
)


def register_all(t: ToolContext) -> dict[str, str]:
    """Register every module; returns each tool's group (its module name),
    the unit of a token's MCP toolsets (roadmap 23.8)."""
    groups: dict[str, str] = {}
    for module in MODULES:
        before = _names(t)
        module.register(t)
        group = module.__name__.rsplit(".", 1)[-1]
        groups.update(dict.fromkeys(_names(t) - before, group))
    return groups


def _names(t: ToolContext) -> set[str]:
    return {tool.name for tool in t.server._tool_manager.list_tools()}
