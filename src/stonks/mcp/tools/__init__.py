"""MCP tool modules, one per safety class. Each exposes
``register(t: ToolContext)``; :data:`MODULES` is the order they register in."""

from __future__ import annotations

from stonks.mcp.tools import (
    calendars,
    cash_flows,
    connections,
    factors,
    guarded,
    halts,
    insights,
    jobs,
    model_versions,
    notifications,
    options,
    orders,
    price_alerts,
    reads,
    research,
    risk,
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
    tax,
    cash_flows,
    factors,
    subscriptions,
    insights,
    risk,
    notifications,
    workspace,
    research,
    calendars,
    screener,
    options,
)


def register_all(t: ToolContext) -> None:
    for module in MODULES:
        module.register(t)
