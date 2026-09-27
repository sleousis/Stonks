"""Notification feed tools: read your in-app feed (signals, fills, halts,
failed runs) and mark items read. Preferences, quiet hours and the webhook
stay in the console: a token must never redirect your alerts."""

# No ``from __future__ import annotations`` (see common.py).

from typing import Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import Field

from stonks.mcp.tools.common import READ, ToolContext, drop_none

#: Marks your own rows read: harmless, and marking twice changes nothing.
MARK = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


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
            Field(max_length=500, description="notification ids; omit to mark all of yours"),
        ] = None,
    ) -> dict[str, Any]:
        """Mark notifications read (all of yours when ids is omitted). Only
        touches your own feed; returns how many changed and the new unread
        count."""
        return await t.post("/api/notifications/read", drop_none({"ids": ids}))
