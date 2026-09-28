"""Cash flow reads (roadmap 20.5): the deposits and withdrawals behind the
time- and money-weighted returns. Recording one moves money in a book, so
it stays in the console and the CLI."""

# No ``from __future__ import annotations`` (see common.py).

from typing import Any

from stonks.mcp.tools.common import READ, Limit, Offset, ToolContext, items, seg


def register(t: ToolContext) -> None:
    server = t.server

    @server.tool(annotations=READ)
    async def list_cash_flows(
        portfolio_id: str, limit: Limit = 200, offset: Offset = 0
    ) -> dict[str, Any]:
        """Every deposit and withdrawal of one of your portfolios, oldest
        first: recorded ones and those a broker sync brought in. Returns take
        them out, so a deposit is never profit."""
        query = {"limit": limit, "offset": offset}
        return items(await t.get(f"/api/portfolios/{seg(portfolio_id)}/cash-flows", query))
