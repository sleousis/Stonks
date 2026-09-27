"""Transaction cost analysis and trade journal tools (BL-32): reads, plus
adding and editing your own journal notes (harmless, no confirm)."""

# No ``from __future__ import annotations`` (see common.py).

from typing import Annotated, Any, Literal
from urllib.parse import quote

from pydantic import Field

from stonks.mcp.tools.common import EDIT, READ, WRITE, ToolContext, add_alias, drop_none

GroupBy = Annotated[
    Literal["all", "strategy", "ticker", "portfolio", "day", "week", "month"],
    Field(description="how to group the orders"),
]
Day = Annotated[str | None, Field(description="YYYY-MM-DD")]
Note = Annotated[str, Field(min_length=1, max_length=4000, description="the note text")]


def register(t: ToolContext) -> None:
    server = t.server

    @server.tool(annotations=READ)
    async def get_tca_summary(
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
    async def list_trade_journal(
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
    async def get_order_tca(client_id: str) -> dict[str, Any]:
        """One of your orders in full: decision price and context, arrival
        and fill prices, the shortfall split and the notes."""
        return await t.get(f"/api/tca/orders/{quote(client_id, safe='')}")

    @server.tool(annotations=WRITE)
    async def add_journal_note(client_id: str, note: Note) -> dict[str, Any]:
        """Add a note to one of your orders in the trade journal (why you
        agree or disagree with it, what you learned). Needs a trading
        token. The note is yours and shows in the console."""
        return await t.post(f"/api/tca/orders/{quote(client_id, safe='')}/notes", {"note": note})

    @server.tool(annotations=EDIT)
    async def edit_journal_note(note_id: Annotated[int, Field(ge=1)], note: Note) -> dict[str, Any]:
        """Replace the text of one of your journal notes."""
        return await t.put(f"/api/tca/notes/{note_id}", {"note": note})

    add_alias(t, get_tca_summary, READ)
    add_alias(t, list_trade_journal, READ)
    add_alias(t, get_order_tca, READ)
