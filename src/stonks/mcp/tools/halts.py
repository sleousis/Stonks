"""Kill switch and risk-halt tools (roadmap 12.6, BL-28): a read-only list,
plus a guarded kill switch. Resuming the kill switch and clearing a halt
turn trading back on, so they stay in the console and the CLI (resuming
needs a typed confirmation, and later a fresh second factor)."""

# No ``from __future__ import annotations`` (see common.py).

from typing import Annotated, Any, Literal

from mcp.types import ToolAnnotations
from pydantic import Field

from stonks.mcp.guards import CONFIRM_HINT
from stonks.mcp.tools.common import READ, Confirm, ToolContext, drop_none, items

#: Stops new orders; engaging again returns the open switch.
KILL = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
)

KillScope = Annotated[
    Literal["global", "user", "portfolio"],
    Field(
        description="global: every portfolio (admins only); user: all of yours; "
        "portfolio: one of yours (give portfolio_id)"
    ),
]


def register(t: ToolContext) -> None:
    server = t.server

    @server.tool(annotations=READ)
    async def list_halts(include_cleared: bool = False) -> dict[str, Any]:
        """Halts you can see, newest first: the kill switch, circuit-breaker
        trips (month loss, week loss, latched drawdown) and the operational
        halt. By default only those in force today."""
        query = {"include_cleared": "true"} if include_cleared else None
        return items(await t.get("/api/halts", params=query))

    @server.tool(annotations=KILL)
    async def engage_kill_switch(
        scope: KillScope,
        reason: Annotated[str, Field(min_length=1, max_length=500, description="audited")],
        portfolio_id: str | None = None,
        flatten: Annotated[
            bool, Field(description="stop buys only; sells and exits still go through")
        ] = False,
        confirm: Confirm = False,
    ) -> dict[str, Any]:
        """Stop new orders at once. Without confirm=true returns a preview
        and changes nothing. Resuming is done in the console or the CLI with
        a typed confirmation."""
        body = drop_none(
            {"scope": scope, "portfolio_id": portfolio_id, "flatten": flatten, "reason": reason}
        )
        if not confirm:
            what = "buys (sells and exits still go through)" if flatten else "every new order"
            target = {
                "global": "every portfolio",
                "user": "all of your portfolios",
                "portfolio": f"portfolio {portfolio_id}",
            }[scope]
            return {
                "preview": True,
                "applied": False,
                "request": body,
                "warnings": [
                    f"stops {what} for {target} from the next tick until resumed",
                    "resuming needs the console or the CLI and the typed confirmation",
                ],
                "next_step": CONFIRM_HINT,
            }
        halt = await t.post("/api/halts/kill", body)
        return {"preview": False, "applied": True, "halt": halt}
