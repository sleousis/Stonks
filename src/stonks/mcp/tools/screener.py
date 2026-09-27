"""Screener tools (roadmap 20.8): run a screen on price and fundamental
metrics, keep your own saved screens, and store a screen as a universe for
the lab. Storing a universe and deleting a saved screen need
``confirm=true``; without it the tool returns a preview."""

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
    IsoDate,
    ToolContext,
    drop_none,
    iso,
    items,
    seg,
)

#: Removes one of your saved screens.
SCREEN_DELETE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
)
#: Stores a new universe definition and queues its refresh.
SCREEN_UNIVERSE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
)

Spec = Annotated[
    dict[str, Any] | None,
    Field(
        description="the screen: rule filters (asset_classes, sectors, exclude_sectors, "
        "exchanges, min_price, min_adv), universe_id to start from a stored universe, "
        'filters [{"metric": "pe_ratio", "min": 0, "max": 15}], sort_by, descending, '
        "limit and columns. Metric ids come from list_screen_metrics"
    ),
]
ScreenId = Annotated[str | None, Field(max_length=64, description="one of your saved screens")]
ScreenName = Annotated[str, Field(min_length=1, max_length=80, description="the screen's name")]


def register(t: ToolContext) -> None:
    server = t.server

    @server.tool(annotations=READ)
    async def list_screen_metrics() -> dict[str, Any]:
        """Every metric a screen may filter or sort on: id, group (price or
        fundamental), unit and what it measures."""
        return items(await t.get("/api/screener/metrics"))

    @server.tool(annotations=READ)
    async def run_screen(
        spec: Spec = None, screen_id: ScreenId = None, as_of: IsoDate | None = None
    ) -> dict[str, Any]:
        """Run a screen (or one of your saved ones) on the lake as it was on
        ``as_of`` (default today): the matching tickers with the value of
        each metric it uses. Only data known on that day counts."""
        body = drop_none({"spec": spec, "screen_id": screen_id, "as_of": iso(as_of)})
        return await t.post("/api/screener/run", body)

    @server.tool(annotations=READ)
    async def list_screens() -> dict[str, Any]:
        """Your saved screens, oldest first."""
        return items(await t.get("/api/screener/screens", params={"limit": 500}))

    @server.tool(annotations=READ)
    async def get_screen(screen_id: str) -> dict[str, Any]:
        """One of your saved screens with its spec."""
        return await t.get(f"/api/screener/screens/{seg(screen_id)}")

    @server.tool(annotations=WRITE)
    async def create_screen(name: ScreenName, spec: Spec = None) -> dict[str, Any]:
        """Save a screen under a name, unique per person. Needs a trading token."""
        return await t.post("/api/screener/screens", {"name": name, "spec": spec or {}})

    @server.tool(annotations=EDIT)
    async def update_screen(
        screen_id: str,
        name: Annotated[str | None, Field(min_length=1, max_length=80)] = None,
        spec: Spec = None,
    ) -> dict[str, Any]:
        """Rename one of your saved screens, replace its spec, or both."""
        body = drop_none({"name": name, "spec": spec})
        return await t.patch(f"/api/screener/screens/{seg(screen_id)}", body)

    @server.tool(annotations=SCREEN_DELETE)
    async def delete_screen(screen_id: str, confirm: Confirm = False) -> dict[str, Any]:
        """Delete one of your saved screens. Universes made from it stay.
        Without confirm=true returns a preview."""
        sid = seg(screen_id)
        if not confirm:
            current = await t.get(f"/api/screener/screens/{sid}")
            return {
                "preview": True,
                "applied": False,
                "action": "delete_screen",
                "target": current,
                "warnings": ["the saved screen is removed for good"],
                "next_step": CONFIRM_HINT,
            }
        await t.delete(f"/api/screener/screens/{sid}")
        return {"preview": False, "applied": True, "deleted": screen_id}

    @server.tool(annotations=SCREEN_UNIVERSE)
    async def save_screen_as_universe(
        universe_id: Annotated[str, Field(description="new universe id, e.g. cheap_tech")],
        spec: Spec = None,
        screen_id: ScreenId = None,
        mode: Annotated[
            Literal["rule", "snapshot"],
            Field(
                description="rule reruns the screen at each rebalance (point in time); "
                "snapshot stores today's matches"
            ),
        ] = "rule",
        start: IsoDate | None = None,
        end: IsoDate | None = None,
        rebalance: Literal["weekly", "monthly", "quarterly"] = "monthly",
        name: str | None = None,
        confirm: Confirm = False,
    ) -> dict[str, Any]:
        """Store a screen as a universe for the lab and queue its refresh
        (follow up with wait_for_job). Without confirm=true returns a preview."""
        body = drop_none(
            {
                "universe_id": universe_id,
                "spec": spec,
                "screen_id": screen_id,
                "mode": mode,
                "start": iso(start),
                "end": iso(end),
                "rebalance": rebalance,
                "name": name,
            }
        )
        if not confirm:
            warnings = ["stores a new universe definition and queues a refresh job"]
            if mode == "snapshot":
                warnings.append("a snapshot carries survivorship bias (P14)")
            return {
                "preview": True,
                "applied": False,
                "action": "save_screen_as_universe",
                "target": body,
                "warnings": warnings,
                "next_step": CONFIRM_HINT,
            }
        saved = await t.post("/api/screener/universes", body)
        return {"preview": False, "applied": True, **saved}
