"""MCP tool modules, one per safety class. Each exposes
``register(t: ToolContext)``; :data:`MODULES` is the order they register in."""

from __future__ import annotations

from stonks.mcp.tools import (
    cash_flows,
    connections,
    factors,
    guarded,
    halts,
    insights,
    jobs,
    notifications,
    orders,
    price_alerts,
    reads,
    research,
    risk,
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
    studio,
    connections,
    halts,
    orders,
    price_alerts,
    universes,
    tca,
    tax,
    cash_flows,
    factors,
    subscriptions,
    insights,
    risk,
    notifications,
    workspace,
    research,
)


def register_all(t: ToolContext) -> None:
    for module in MODULES:
        module.register(t)
