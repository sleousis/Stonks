"""MCP tool modules, one per safety class. Each exposes
``register(t: ToolContext)``; :data:`MODULES` is the order they register in."""

from __future__ import annotations

from stonks.mcp.tools import guarded, jobs, reads, studio
from stonks.mcp.tools.common import ToolContext

MODULES = (reads, jobs, guarded, studio)


def register_all(t: ToolContext) -> None:
    for module in MODULES:
        module.register(t)
