"""Universe tools: read stored universes and their point-in-time members,
plus guarded writes (create, refresh, ensure data, index import, delete).
Every write needs ``confirm=true``; without it the tool returns a preview
and sends nothing mutating."""

# No ``from __future__ import annotations`` (see common.py).

from typing import Annotated, Any, Literal

from mcp.types import ToolAnnotations
from pydantic import Field

from stonks.mcp.guards import CONFIRM_HINT
from stonks.mcp.tools.common import (
    READ,
    Confirm,
    IsoDate,
    ToolContext,
    drop_none,
    iso,
    items,
    seg,
)

#: Changes which names a universe holds; repeating the same write converges.
UNIVERSE_WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
)
#: Removes a definition and its membership rows; lab runs and ticks that
#: name it stop resolving.
UNIVERSE_DELETE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
)
#: Fetches from a vendor and writes the lake (membership rows or bars).
UNIVERSE_FETCH = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True
)

UniverseId = Annotated[str, Field(description="universe id, e.g. sp500 or us_common")]
Kind = Literal["list", "exchange", "rule", "index"]
Spec = Annotated[
    dict[str, Any] | None,
    Field(
        description="kind settings. list: {tickers, spans}. exchange: {exchange, source, "
        "security_types, include_delisted}. rule: {start, end, rebalance, min_adv, min_price, "
        "asset_classes, sectors, exclude_sectors, exchanges}. index: {index_id, source, "
        "start_date}"
    ),
]


def _preview(action: str, target: Any, warnings: list[str]) -> dict[str, Any]:
    return {
        "preview": True,
        "applied": False,
        "action": action,
        "target": target,
        "warnings": warnings,
        "next_step": CONFIRM_HINT,
    }


def register(t: ToolContext) -> None:
    server = t.server

    @server.tool(annotations=READ)
    async def list_universes() -> dict[str, Any]:
        """Stored universes: id, kind, spec, last refresh and member count."""
        return items(await t.get("/api/universes"))

    @server.tool(annotations=READ)
    async def get_universe(universe_id: UniverseId) -> dict[str, Any]:
        """One stored universe definition."""
        return await t.get(f"/api/universes/{seg(universe_id)}")

    @server.tool(annotations=READ)
    async def get_universe_members(
        universe_id: UniverseId, as_of: IsoDate | None = None
    ) -> dict[str, Any]:
        """Members of a universe on a date (default today), point in time:
        names that later died are members on the days they were listed."""
        return await t.get(
            f"/api/universes/{seg(universe_id)}/members", drop_none({"as_of": iso(as_of)})
        )

    @server.tool(annotations=UNIVERSE_WRITE)
    async def create_universe(
        universe_id: UniverseId,
        kind: Kind,
        spec: Spec = None,
        name: str | None = None,
        description: str | None = None,
        csv: Annotated[
            str | None, Field(description="list only: CSV with a ticker column (replaces spec)")
        ] = None,
        confirm: Confirm = False,
    ) -> dict[str, Any]:
        """Store a universe definition. It has no members until refreshed
        (refresh_universe). Without confirm=true returns a preview."""
        body = drop_none(
            {
                "id": universe_id,
                "kind": kind,
                "spec": spec or {},
                "name": name,
                "description": description,
                "csv": csv,
            }
        )
        if not confirm:
            warnings = []
            if kind == "list":
                warnings.append("a plain ticker list carries survivorship bias (P14)")
            return _preview("create", body, warnings)
        created = await t.post("/api/universes", body)
        return {"preview": False, "applied": True, "universe": created}

    @server.tool(annotations=UNIVERSE_FETCH)
    async def refresh_universe(universe_id: UniverseId, confirm: Confirm = False) -> dict[str, Any]:
        """Queue a refresh that rebuilds the universe's membership (an
        exchange universe lists symbols at the data source). Follow up with
        wait_for_job. Without confirm=true returns a preview."""
        uid = seg(universe_id)
        current = await t.get(f"/api/universes/{uid}")
        if not confirm:
            warnings = ["replaces the universe's membership rows"]
            if current.get("kind") == "exchange":
                warnings.append("lists every symbol on the exchange at the data source")
            return _preview("refresh", current, warnings)
        job = await t.post(f"/api/universes/{uid}/refresh")
        return {"preview": False, "applied": True, "job": job}

    @server.tool(annotations=UNIVERSE_FETCH)
    async def ensure_universe_data(
        universe_id: UniverseId,
        start: IsoDate,
        end: IsoDate,
        interval: str = "1d",
        source: Literal["eodhd", "yahoo", "defillama"] | None = None,
        confirm: Confirm = False,
    ) -> dict[str, Any]:
        """Queue a job that fetches only the missing bars of every member
        over the window (delisted names included) from the data source.
        Follow up with wait_for_job. Without confirm=true returns a preview."""
        uid = seg(universe_id)
        body = drop_none(
            {"start": iso(start), "end": iso(end), "interval": interval, "source": source}
        )
        if not confirm:
            current = await t.get(f"/api/universes/{uid}")
            return _preview(
                "ensure_data",
                {"universe": current, **body},
                ["fetches market data from the vendor; the EODHD free tier keeps one year"],
            )
        job = await t.post(f"/api/universes/{uid}/ensure", body)
        return {"preview": False, "applied": True, "job": job}

    @server.tool(annotations=UNIVERSE_WRITE)
    async def import_index_history(
        index_id: Annotated[str, Field(description="lower_snake_case index code, e.g. sp500")],
        content: Annotated[str, Field(description="the CSV or JSON text")],
        format: Literal["csv", "json"] = "csv",
        confirm: Confirm = False,
    ) -> dict[str, Any]:
        """Import an index's constituents and changes. CSV columns
        date,ticker,action with action add, remove or member. Without
        confirm=true returns a preview."""
        body = {"index_id": index_id, "format": format, "content": content}
        if not confirm:
            return _preview(
                "import_index_history",
                {"index_id": index_id, "format": format, "bytes": len(content)},
                ["adds to the stored history of this index"],
            )
        result = await t.post("/api/universes/index-history", body)
        return {"preview": False, "applied": True, "result": result}

    @server.tool(annotations=UNIVERSE_DELETE)
    async def delete_universe(universe_id: UniverseId, confirm: Confirm = False) -> dict[str, Any]:
        """Delete a stored universe and its membership rows (admins only).
        Lab runs, ticks and scheduled jobs that name it stop resolving.
        Without confirm=true returns a preview."""
        uid = seg(universe_id)
        current = await t.get(f"/api/universes/{uid}")
        if not confirm:
            return _preview(
                "delete",
                current,
                [
                    "removes the definition and every membership row",
                    "lab runs and ticks that name this universe stop resolving",
                ],
            )
        deleted = await t.delete(f"/api/universes/{uid}")
        return {"preview": False, "applied": True, "universe": deleted}
