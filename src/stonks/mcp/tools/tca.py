"""Transaction cost analysis and trade journal tools (BL-32): read-only.
Journal notes are written in the console or the CLI."""

# No ``from __future__ import annotations`` (see common.py).

from typing import Annotated, Any, Literal
from urllib.parse import quote

from pydantic import Field

from stonks.mcp.tools.common import READ, ToolContext, drop_none

GroupBy = Annotated[
    Literal["all", "strategy", "ticker", "portfolio", "day", "week", "month"],
    Field(description="how to group the orders"),
]
Day = Annotated[str | None, Field(description="YYYY-MM-DD")]


def register(t: ToolContext) -> None:
    server = t.server

    @server.tool(annotations=READ)
    async def tca_summary(
        by: GroupBy = "all",
        since: Day = None,
        until: Day = None,
        strategy_id: str | None = None,
        ticker: str | None = None,
        portfolio_id: str | None = None,
    ) -> dict[str, Any]:
        """Implementation shortfall of your orders in bps of notional:
        delay (decision to arrival), impact (arrival to fill), fees and the
        opportunity cost of what did not fill, next to the cost model's
        estimate (model_gap_bps = realised minus modelled)."""
        query = drop_none(
            {
                "by": by,
                "since": since,
                "until": until,
                "strategy_id": strategy_id,
                "ticker": ticker,
                "portfolio_id": portfolio_id,
            }
        )
        return await t.get("/api/tca/summary", params=query)

    @server.tool(annotations=READ)
    async def trade_journal(
        since: Day = None,
        strategy_id: str | None = None,
        ticker: str | None = None,
        portfolio_id: str | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> dict[str, Any]:
        """Your orders, newest first: why each was placed (trigger, signal
        score and rank), the outcome (shortfall, next-day move) and notes."""
        query = drop_none(
            {
                "since": since,
                "strategy_id": strategy_id,
                "ticker": ticker,
                "portfolio_id": portfolio_id,
                "limit": limit,
                "offset": offset,
            }
        )
        return await t.get("/api/tca/journal", params=query)

    @server.tool(annotations=READ)
    async def order_tca(client_id: str) -> dict[str, Any]:
        """One of your orders in full: decision price and context, arrival
        and fill prices, the shortfall split and the notes."""
        return await t.get(f"/api/tca/orders/{quote(client_id, safe='')}")
