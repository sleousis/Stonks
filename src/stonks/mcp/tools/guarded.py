"""Guarded write tools: strategy status changes and ``run_tick``. Each
needs ``confirm=true``; without it they return a preview and send nothing
mutating."""

# No ``from __future__ import annotations`` (see common.py).

from datetime import date
from typing import Annotated, Any

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from stonks.mcp.client import ApiClient, ApiError
from stonks.mcp.guards import CONFIRM_HINT, live_trading_state, status_change_preview
from stonks.mcp.tools.common import (
    STATUS_CHANGE,
    TICK,
    AssetClass,
    Confirm,
    ToolContext,
    drop_none,
    iso,
    seg,
)

#: Broker the production tick trades through (``BrokerInfo``). A
#: non-dry-run tick needs it to report a simulated or paper broker.
BROKER_STATUS_PATH = "/api/brokers"


def register(t: ToolContext) -> None:
    server = t.server

    async def change_status(strategy_id: str, action: str, target: str, confirm: bool):
        sid = seg(strategy_id)
        current = await t.get(f"/api/strategies/{sid}")
        if not confirm:
            return status_change_preview(current, target)
        updated = await t.post(f"/api/strategies/{sid}/{action}")
        return {
            "preview": False,
            "applied": True,
            "previous_status": current.get("status"),
            "strategy": updated,
        }

    @server.tool(annotations=STATUS_CHANGE)
    async def promote_strategy(strategy_id: str, confirm: Confirm = False) -> dict[str, Any]:
        """Promote a strategy to active so production ticks rank and trade it.
        Without confirm=true returns a preview (current status, survival
        results, warnings) and changes nothing."""
        return await change_status(strategy_id, "promote", "active", confirm)

    @server.tool(annotations=STATUS_CHANGE)
    async def shadow_strategy(strategy_id: str, confirm: Confirm = False) -> dict[str, Any]:
        """Move a strategy to shadow: evaluated on a virtual portfolio, never traded.
        Without confirm=true returns a preview and changes nothing."""
        return await change_status(strategy_id, "shadow", "shadow", confirm)

    @server.tool(annotations=STATUS_CHANGE)
    async def retire_strategy(strategy_id: str, confirm: Confirm = False) -> dict[str, Any]:
        """Retire a strategy: it stops being ranked or evaluated.
        Without confirm=true returns a preview and changes nothing."""
        return await change_status(strategy_id, "retire", "retired", confirm)

    @server.tool(annotations=TICK)
    async def run_tick(
        confirm: Confirm = False,
        dry_run: Annotated[
            bool, Field(description="true (default): rank and log only, place no orders")
        ] = True,
        as_of: Annotated[date | None, Field(description="YYYY-MM-DD; default today")] = None,
        tickers: Annotated[
            list[str] | None, Field(description="override [production].universe")
        ] = None,
        asset_class: AssetClass | None = None,
    ) -> dict[str, Any]:
        """Queue one production tick (rank active strategies, then trade the winner).
        Dry run by default. Without confirm=true returns a preview and queues
        nothing. A real tick (dry_run=false) is refused unless GET /api/brokers
        reports a simulated or paper broker."""
        body = drop_none(
            {
                "dry_run": dry_run,
                "as_of": iso(as_of),
                "tickers": tickers,
                "asset_class": asset_class,
            }
        )
        live = "not checked (dry run)"
        if not dry_run:
            live = await _broker_live_state(t.api)
        if not confirm:
            active = await t.get("/api/strategies", {"status": "active", "limit": 500})
            warnings = []
            if not dry_run and live != "off":
                warnings.append(_live_refusal(live))
            if not active.get("items"):
                warnings.append("no active strategies: the tick will place no orders")
            return {
                "preview": True,
                "applied": False,
                "request": body,
                "universe": tickers or "server default ([production].universe)",
                "active_strategies": [s.get("id") for s in active.get("items", [])],
                "live_trading": live,
                "warnings": warnings,
                "next_step": CONFIRM_HINT,
            }
        if not dry_run and live != "off":
            raise ToolError(_live_refusal(live))
        job = await t.post("/api/ticks", body)
        return {"preview": False, "applied": True, "job": job}


async def _broker_live_state(api: ApiClient) -> str:
    try:
        info = await api.get(BROKER_STATUS_PATH)
    except ApiError:
        return "unknown"
    return live_trading_state(info)


def _live_refusal(state: str) -> str:
    if state == "on":
        return (
            "refusing a non-dry-run tick: the API's broker is not simulated or paper, "
            "so it trades real money. Run live ticks from the CLI or UI, not through MCP."
        )
    return (
        "refusing a non-dry-run tick: the API did not report a simulated or paper broker, so "
        "MCP cannot rule out live trading. Use dry_run=true, or the CLI/UI."
    )
