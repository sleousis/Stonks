"""Live risk monitoring tools (BL-47): read-only, scoped to your portfolios
by the API."""

# No ``from __future__ import annotations`` (see common.py).

from typing import Annotated, Any

from pydantic import Field

from stonks.mcp.tools.common import READ, ToolContext, drop_none

Day = Annotated[str | None, Field(description="YYYY-MM-DD")]


def register(t: ToolContext) -> None:
    server = t.server

    @server.tool(annotations=READ)
    async def live_risk(portfolio_id: str | None = None) -> dict[str, Any]:
        """One of your portfolios on its latest tick day: one-day 95% and
        99% VaR and expected shortfall (fractions of value, loss positive),
        the rolling VaR violation ratio (1.0 is right, outside 0.5 to 1.5
        means the risk model is off) with its Kupiec p-value, and per
        strategy sleeve the same plus the alpha-decay check against the
        backtest's IR."""
        return await t.get("/api/risk/live", params=drop_none({"portfolio_id": portfolio_id}))

    @server.tool(annotations=READ)
    async def risk_snapshots(
        portfolio_id: str | None = None,
        strategy_id: Annotated[
            str | None, Field(description="one strategy's sleeve; default the whole portfolio")
        ] = None,
        since: Day = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> dict[str, Any]:
        """Daily risk snapshots of one of your portfolios, newest first:
        VaR, ES, the day's hypothetical return, violations and decay."""
        query = drop_none(
            {
                "portfolio_id": portfolio_id,
                "strategy_id": strategy_id,
                "since": since,
                "limit": limit,
                "offset": offset,
            }
        )
        return await t.get("/api/risk/snapshots", params=query)
