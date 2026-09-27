"""MCP tool modules, one per safety class. Each exposes
``register(t: ToolContext)``; :data:`MODULES` is the order they register in."""

from __future__ import annotations

from stonks.mcp.tools import (
    cash_flows,
    connections,
    guarded,
    halts,
    insights,
    jobs,
    model_versions,
    notifications,
    orders,
    price_alerts,
    reads,
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
    model_versions,
    studio,
    connections,
    halts,
    orders,
    price_alerts,
    universes,
    tca,
    tax,
    cash_flows,
    subscriptions,
    insights,
    risk,
    notifications,
    workspace,
)


def register_all(t: ToolContext) -> None:
    for module in MODULES:
        module.register(t)
