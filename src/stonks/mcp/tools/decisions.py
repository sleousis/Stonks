"""Why did or didn't we trade (roadmap 23.7): read-only, scoped to your
portfolios by the API."""

# No ``from __future__ import annotations`` (see common.py).

from typing import Annotated, Any

from pydantic import Field

from stonks.mcp.tools.common import READ, ToolContext, drop_none


def register(t: ToolContext) -> None:
    @t.server.tool(annotations=READ)
    async def list_trade_decisions(
        ticker: Annotated[
            str | None, Field(max_length=64, description="one ticker, e.g. AAPL.US")
        ] = None,
        portfolio_id: str | None = None,
        strategy_id: Annotated[
            str | None, Field(max_length=200, description="rows this strategy owned or scored")
        ] = None,
        tick_id: Annotated[str | None, Field(max_length=64, description="one tick")] = None,
        since: Annotated[str | None, Field(description="YYYY-MM-DD")] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 20,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> dict[str, Any]:
        """Why a ticker did or did not trade in one of your portfolios,
        newest first. Each row names the step that kept it out or trimmed
        it: universe, rank (another pick won), constructor (no weight),
        buffer (inside the no-trade band), stale_price, risk_rule (with the
        rule and the quantities), lots (rounded to tradable lots or
        skipped below one lot), scope, external, halt, held or traded.
        Ask with a ticker to answer "why didn't we buy X"."""
        query = drop_none(
            {
                "ticker": ticker,
                "portfolio_id": portfolio_id,
                "strategy_id": strategy_id,
                "tick_id": tick_id,
                "since": since,
                "limit": limit,
                "offset": offset,
            }
        )
        return await t.get("/api/decisions", params=query)
