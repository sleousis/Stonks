"""Broker-connection tools: read-only listings, plus a guarded manual sync.
Connecting, linking and removing a broker stay in the console (they handle
credentials, and later need a fresh second factor)."""

# No ``from __future__ import annotations`` (see common.py).

from typing import Any

from mcp.types import ToolAnnotations

from stonks.mcp.guards import CONFIRM_HINT
from stonks.mcp.tools.common import READ, Confirm, ToolContext, items, seg

#: Reads from the broker (read-only there) and overwrites today's synced
#: snapshot of the linked portfolios; repeating it converges.
SYNC = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True
)


def register(t: ToolContext) -> None:
    server = t.server

    @server.tool(annotations=READ)
    async def list_connections() -> dict[str, Any]:
        """Your broker connections: provider, status, last sync and error.
        Never includes credentials."""
        return items(await t.get("/api/connections"))

    @server.tool(annotations=READ)
    async def get_connection_accounts(connection_id: str) -> dict[str, Any]:
        """External accounts on one of your connections, each with the
        portfolio that mirrors it (null when not linked)."""
        return items(await t.get(f"/api/connections/{seg(connection_id)}/accounts"))

    @server.tool(annotations=SYNC)
    async def sync_connection(connection_id: str, confirm: Confirm = False) -> dict[str, Any]:
        """Sync one of your connections now: read balances, positions and
        activity from the provider (read-only there) into the linked broker
        portfolios. Without confirm=true returns a preview and syncs nothing."""
        cid = seg(connection_id)
        connection = await t.get(f"/api/connections/{cid}")
        if not confirm:
            return {
                "preview": True,
                "applied": False,
                "connection": connection,
                "warnings": [
                    f"reads from {connection.get('provider')} (read-only at the provider) and "
                    "replaces today's synced snapshot of every linked portfolio"
                ],
                "next_step": CONFIRM_HINT,
            }
        result = await t.post(f"/api/connections/{cid}/sync")
        return {"preview": False, "applied": True, "result": result}
