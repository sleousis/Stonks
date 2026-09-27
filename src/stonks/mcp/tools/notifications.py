"""Notification tools: read your in-app feed (signals, fills, halts,
failed runs) and mark items read, read your notification settings, and
turn the upcoming-event alert kinds (earnings, dividends, economic
releases) on or off and pick the countries and importance of economic
release alerts with ``confirm=true``. Channel switches, quiet hours
and the webhook stay in the console: a token must never redirect your
alerts or silence risk alerts."""

# No ``from __future__ import annotations`` (see common.py).

from typing import Annotated, Any, Literal

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from stonks.mcp.guards import CONFIRM_HINT
from stonks.mcp.tools.common import READ, Confirm, ToolContext, drop_none

#: Marks your own rows read: harmless, and marking twice changes nothing.
MARK = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
#: Turns a kind of your own alerts off or on: guarded, repeatable.
SWITCH = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
)

Switch = Annotated[bool | None, Field(description="true: on, false: off, omit: leave as is")]


def register(t: ToolContext) -> None:
    server = t.server

    @server.tool(annotations=READ)
    async def list_notifications(
        unread_only: bool = False,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
        before_id: Annotated[
            int | None, Field(ge=1, description="page: only items with a lower id")
        ] = None,
    ) -> dict[str, Any]:
        """Your notification feed, newest first, with the unread count: what
        Stonks told you (signals, fills, halts, failed runs), each with a
        category, level, message and a link into the console."""
        query = drop_none(
            {"unread_only": unread_only or None, "limit": limit, "before_id": before_id}
        )
        return await t.get("/api/notifications", params=query)

    @server.tool(annotations=MARK)
    async def mark_notifications_read(
        ids: Annotated[
            list[int] | None,
            Field(max_length=500, description="notification ids, or omit to mark all of yours"),
        ] = None,
    ) -> dict[str, Any]:
        """Mark notifications read (all of yours when ids is omitted). Only
        touches your own feed, and returns how many changed and the new unread
        count."""
        return await t.post("/api/notifications/read", drop_none({"ids": ids}))

    @server.tool(annotations=READ)
    async def get_notification_preferences() -> dict[str, Any]:
        """Your notification settings: which categories (signals, orders,
        risk, system, price alerts, event alerts) go to which channel, the
        upcoming-event alert kinds you get, and your quiet hours. The webhook
        shows its host only."""
        return await t.get("/api/notifications/preferences")

    @server.tool(annotations=SWITCH)
    async def set_event_alerts(
        earnings: Switch = None,
        dividends: Switch = None,
        economic: Switch = None,
        economic_countries: Annotated[
            list[str] | None,
            Field(
                max_length=30,
                description="countries for economic release alerts, such as US, EU, DE",
            ),
        ] = None,
        economic_default_countries: Annotated[
            bool,
            Field(description="true: follow your portfolios' base currencies again"),
        ] = False,
        economic_importance: Annotated[
            Literal["low", "medium", "high"] | None,
            Field(description="the lowest importance of release that alerts you"),
        ] = None,
        confirm: Confirm = False,
    ) -> dict[str, Any]:
        """Turn kinds of upcoming-event alerts on or off for yourself:
        earnings coming up, ex-dividend dates coming up, economic releases
        coming up. Off means none of that kind, not even in the app. Also
        picks the countries and the importance threshold (low, medium, high)
        of economic release alerts. Without confirm=true returns your
        current choices and changes nothing."""
        wanted = drop_none({"earnings": earnings, "dividends": dividends, "economic": economic})
        econ = drop_none(
            {
                "countries": economic_countries,
                "default_countries": economic_default_countries or None,
                "min_importance": economic_importance,
            }
        )
        if not wanted and not econ:
            raise ToolError(
                "set at least one of earnings, dividends, economic or an economic_* choice"
            )
        if not confirm:
            current = await t.get("/api/notifications/preferences")
            return {
                "preview": True,
                "applied": False,
                "event_alerts": current["event_alerts"],
                "economic_alerts": _economic_view(current),
                "would_set": {**wanted, **({"economic_alerts": econ} if econ else {})},
                "next_step": CONFIRM_HINT,
            }
        body: dict[str, Any] = {
            "event_alerts": [{"topic": k, "enabled": v} for k, v in wanted.items()]
        }
        if econ:
            body["economic_alerts"] = econ
        updated = await t.put("/api/notifications/preferences", body)
        return {
            "preview": False,
            "applied": True,
            "event_alerts": updated["event_alerts"],
            "economic_alerts": _economic_view(updated),
        }


def _economic_view(prefs: dict[str, Any]) -> dict[str, Any] | None:
    """The economic choices without the long option lists."""
    econ = prefs.get("economic_alerts")
    if not econ:
        return None
    return {k: econ[k] for k in ("countries", "default_countries", "min_importance")}
