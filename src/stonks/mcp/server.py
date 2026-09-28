"""MCP tools and resources over the Stonks REST API (official ``mcp`` SDK).

Tools live in :mod:`stonks.mcp.tools`, one module per area (``reads``,
``jobs``, ``guarded``, ``studio``, ``connections``, ``notifications``, ...).
Reads are ``get_*`` / ``list_*`` and queued work is ``run_*``. Renamed tools
keep their old names as deprecated aliases (``ALIASES`` in ``common.py``). Each tool is a small async
function over :class:`~stonks.mcp.tools.common.ToolContext` returning plain
JSON-able data; a parameterless read route is just a ``RouteRead`` row.
``mcp`` types stay inside ``stonks.mcp``.

Safety model
------------
- Read tools only issue GETs (plus pure validation POSTs that save nothing).
- Job tools (backtest, lab run, signal IC, ingest, draft backtest/lab run) queue
  background work on the API; they write research data only, never orders.
- Guarded tools (strategy status changes, draft register/enable/disable,
  run_tick) need ``confirm=true``; without it they return a preview and
  send nothing mutating. ``run_tick`` defaults to ``dry_run=true``, and a
  real tick is refused unless ``GET /api/brokers`` reports a simulated or
  paper broker. ``sync_connection`` (read-only at the broker) needs
  ``confirm=true`` too; connecting, linking or removing a broker is
  console-only.
- No tool changes broker settings, configuration, or enables live trading.
"""

import json
from collections.abc import Collection

from mcp.server import MCPServer

from stonks.mcp.client import ApiClient
from stonks.mcp.tools import register_all
from stonks.mcp.tools.common import ToolContext
from stonks.mcp.toolsets import allowed

INSTRUCTIONS = """Stonks research + trading system. Tools talk to the local REST API started
with `stonks serve`. Read tools are safe. Job tools queue backtests, lab runs,
sweeps, signal IC analyses and ingests. Follow up with wait_for_job, and stop
one with cancel_job. Check get_golive_report before promote_strategy, and
check_model_swap before swap_model_version. Status changes, model swaps,
draft register/enable/disable, broker syncs, manual orders (place_order,
change_order, cancel_order) and production ticks need confirm=true; call them
first without it to get a preview and show it to the user before confirming.
run_tick is a dry run unless dry_run=false, and a real tick is refused unless
the API reports a paper/simulated broker. No tool can change broker settings
or enable live trading.
Every tool acts as the user who owns this server's API token (whoami shows the
user, role and scopes) and sees only that user's portfolios. Actions that need
a fresh second factor (switching to auto, connecting a broker, resuming the
kill switch, restoring a backup, a manual order on a real-money book) are
refused here. The user does them in the web app."""


def build_server(
    api: ApiClient,
    *,
    max_wait_seconds: float = 600.0,
    toolsets: Collection[str] | None = None,
) -> MCPServer:
    """``toolsets``: the tool groups the token may use (roadmap 23.8). The
    tools of other groups are not registered at all. None keeps every tool."""
    server = MCPServer("stonks", instructions=INSTRUCTIONS)
    t = ToolContext(server, api, max_wait_seconds)
    groups = register_all(t)
    if toolsets is not None:
        for name, group in groups.items():
            if not allowed(name, group, toolsets):
                server.remove_tool(name)
    _register_resources(t)
    return server


# --- resources --------------------------------------------------------------------


def _register_resources(t: ToolContext) -> None:
    @t.server.resource(
        "stonks://portfolio/summary",
        name="portfolio_summary",
        description="Cash, total value and positions of the current portfolio",
        mime_type="application/json",
    )
    async def portfolio_summary() -> str:
        p = await t.get("/api/portfolio")
        summary = {
            "taken_at": p.get("taken_at"),
            "tick_id": p.get("tick_id"),
            "cash": p.get("cash"),
            "positions_value": p.get("positions_value"),
            "total_value": p.get("total_value"),
            "positions": [
                {k: pos.get(k) for k in ("ticker", "quantity", "market_value", "weight")}
                for pos in p.get("positions", [])
            ],
        }
        return json.dumps(summary)


# --- entrypoint --------------------------------------------------------------------


def run_stdio(
    api: ApiClient,
    *,
    max_wait_seconds: float = 600.0,
    toolsets: Collection[str] | None = None,
) -> None:
    """Serve over stdio until the client disconnects (blocking)."""
    build_server(api, max_wait_seconds=max_wait_seconds, toolsets=toolsets).run("stdio")
