"""Price alert tools (roadmap 20.2): your own rules on a ticker or a
watchlist, and when they fired. Creating and changing a rule is a small
write. Deleting one needs ``confirm=true``."""

# No ``from __future__ import annotations`` (see common.py).

from typing import Annotated, Any, Literal

from mcp.types import ToolAnnotations
from pydantic import Field

from stonks.mcp.guards import CONFIRM_HINT
from stonks.mcp.tools.common import (
    EDIT,
    READ,
    WRITE,
    Confirm,
    Limit,
    Offset,
    ToolContext,
    drop_none,
    items,
    seg,
)

#: Deletes a rule for good.
DELETE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
)

Level = Annotated[float | None, Field(gt=0, description="the price to watch (crossings)")]
Pct = Annotated[float | None, Field(gt=0, le=1000, description="percent move, either way")]
Window = Annotated[
    int | None, Field(ge=1, le=365, description="calendar days the move is measured over")
]


def register(t: ToolContext) -> None:
    server = t.server

    @server.tool(annotations=READ)
    async def list_price_alerts(limit: Limit = 100, offset: Offset = 0) -> dict[str, Any]:
        """Your price alert rules, with the price each last saw per ticker."""
        return items(await t.get("/api/price-alerts", {"limit": limit, "offset": offset}))

    @server.tool(annotations=READ)
    async def list_price_alert_events(
        rule_id: str | None = None, limit: Limit = 50, offset: Offset = 0
    ) -> dict[str, Any]:
        """When your price alerts fired, newest first."""
        query = drop_none({"rule_id": rule_id, "limit": limit, "offset": offset})
        return items(await t.get("/api/price-alerts/events", query))

    @server.tool(annotations=WRITE)
    async def create_price_alert(
        condition: Annotated[
            Literal["crosses_above", "crosses_below", "moves_pct"],
            Field(description="crosses_above or crosses_below a level, or moves_pct"),
        ],
        ticker: Annotated[str | None, Field(description="one instrument id, e.g. AAPL.US")] = None,
        watchlist_id: Annotated[
            str | None, Field(description="or every ticker of one of your watchlists")
        ] = None,
        level: Level = None,
        pct: Pct = None,
        window_days: Window = None,
        name: Annotated[str | None, Field(max_length=80)] = None,
    ) -> dict[str, Any]:
        """Create a price alert on a ticker or one of your watchlists. It is
        checked after each data refresh and sent through your notification
        channels (push, email, Telegram) with your quiet hours."""
        body = drop_none(
            {
                "condition": condition,
                "ticker": ticker,
                "watchlist_id": watchlist_id,
                "level": level,
                "pct": pct,
                "window_days": window_days,
                "name": name,
            }
        )
        return await t.post("/api/price-alerts", body)

    @server.tool(annotations=EDIT)
    async def update_price_alert(
        alert_id: str,
        level: Level = None,
        pct: Pct = None,
        window_days: Window = None,
        name: Annotated[str | None, Field(max_length=80)] = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        """Change a price alert's thresholds or name, or switch it on or off."""
        body = drop_none(
            {
                "level": level,
                "pct": pct,
                "window_days": window_days,
                "name": name,
                "enabled": enabled,
            }
        )
        return await t.patch(f"/api/price-alerts/{seg(alert_id)}", body)

    @server.tool(annotations=DELETE)
    async def delete_price_alert(alert_id: str, confirm: Confirm = False) -> dict[str, Any]:
        """Delete one of your price alerts. Without confirm=true returns the
        rule and deletes nothing."""
        aid = seg(alert_id)
        if not confirm:
            rule = await t.get(f"/api/price-alerts/{aid}")
            return {"preview": True, "applied": False, "rule": rule, "next_step": CONFIRM_HINT}
        await t.delete(f"/api/price-alerts/{aid}")
        return {"preview": False, "applied": True, "deleted": aid}
