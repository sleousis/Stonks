"""Screen alert tools (roadmap 23.17): a saved screen that runs on a
schedule and notifies you about names that newly match. Turning an alert
on or changing it is a small edit. Removing one needs ``confirm=true``."""

# No ``from __future__ import annotations`` (see common.py).

from typing import Annotated, Any, Literal

from mcp.types import ToolAnnotations
from pydantic import Field

from stonks.mcp.guards import CONFIRM_HINT
from stonks.mcp.tools.common import (
    EDIT,
    READ,
    Confirm,
    Limit,
    Offset,
    ToolContext,
    drop_none,
    items,
    seg,
)

#: Removes the alert from a screen (the screen stays).
ALERT_DELETE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
)


def register(t: ToolContext) -> None:
    server = t.server

    @server.tool(annotations=READ)
    async def list_screen_alerts(limit: Limit = 100, offset: Offset = 0) -> dict[str, Any]:
        """Your screen alerts: which saved screens alert, daily or weekly,
        the last day each ran and how many names it matched."""
        return items(await t.get("/api/screener/alerts", {"limit": limit, "offset": offset}))

    @server.tool(annotations=READ)
    async def list_screen_alert_events(
        screen_id: str | None = None, limit: Limit = 50, offset: Offset = 0
    ) -> dict[str, Any]:
        """When your screens found names that newly match, newest first."""
        query = drop_none({"screen_id": screen_id, "limit": limit, "offset": offset})
        return items(await t.get("/api/screener/alerts/events", query))

    @server.tool(annotations=EDIT)
    async def set_screen_alert(
        screen_id: Annotated[str, Field(description="one of your saved screens")],
        cadence: Annotated[
            Literal["daily", "weekly"], Field(description="run on every trading day, or weekly")
        ] = "daily",
        weekday: Annotated[
            int | None, Field(ge=0, le=6, description="weekly: the day it runs, 0 = Monday")
        ] = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        """Turn on (or change) the alert of a saved screen. It runs after
        each data refresh and notifies you about names that newly match,
        through your notification channels. Notify only: nothing trades.
        The first run stores the matches and sends nothing."""
        body = drop_none({"cadence": cadence, "weekday": weekday, "enabled": enabled})
        return await t.put(f"/api/screener/screens/{seg(screen_id)}/alert", body)

    @server.tool(annotations=ALERT_DELETE)
    async def delete_screen_alert(screen_id: str, confirm: Confirm = False) -> dict[str, Any]:
        """Remove the alert from one of your saved screens (the screen
        stays). Without confirm=true returns the alert and removes nothing."""
        sid = seg(screen_id)
        if not confirm:
            alert = await t.get(f"/api/screener/screens/{sid}/alert")
            return {"preview": True, "applied": False, "alert": alert, "next_step": CONFIRM_HINT}
        await t.delete(f"/api/screener/screens/{sid}/alert")
        return {"preview": False, "applied": True, "deleted": sid}
