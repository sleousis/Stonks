"""Portfolio insight tools (roadmap 15.4): read-only views of one of your
portfolios, synced broker accounts included, plus admin totals."""

# No ``from __future__ import annotations`` (see common.py).

from typing import Annotated, Any

from pydantic import Field

from stonks.mcp.tools.common import READ, PortfolioId, RouteRead, ToolContext, register_route_reads

ROUTE_READS: tuple[RouteRead, ...] = (
    RouteRead(
        "get_insights_totals",
        "/api/insights/totals",
        "Admins only: asset-class allocation and exposure summed over every active portfolio "
        "(no tickers, no per-person numbers).",
    ),
)

Benchmark = Annotated[
    str | None,
    Field(
        max_length=32,
        pattern=r"^[A-Za-z0-9_.\-]+$",
        description="ticker beta is measured against; default SPY.US",
    ),
]


def register(t: ToolContext) -> None:
    register_route_reads(t, ROUTE_READS)
    server = t.server

    @server.tool(annotations=READ)
    async def get_insights(
        portfolio_id: PortfolioId = None, benchmark: Benchmark = None
    ) -> dict[str, Any]:
        """Insights for one of your portfolios (a synced broker account too):
        allocation by asset class, sector, currency and ticker; exposure
        (gross, net, beta); P&L over 1d, 1w, 1m, 3m, ytd, 1y and since
        inception; risk (volatility, drawdown, VaR, concentration)."""
        return await t.get("/api/insights", {"portfolio_id": portfolio_id, "benchmark": benchmark})

    @server.tool(annotations=READ)
    async def get_behaviour_report(
        portfolio_id: PortfolioId = None,
        since: Annotated[str | None, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")] = None,
    ) -> dict[str, Any]:
        """How you trade by hand in one of your portfolios: manual and synced
        broker trades as round trips, with P&L by holding time and weekday,
        win rate, the disposition effect, overtrading, revenge trades after
        a loss, and what trading against the active strategies cost."""
        query = {"portfolio_id": portfolio_id, "since": since}
        return await t.get("/api/insights/behaviour", {k: v for k, v in query.items() if v})

    @server.tool(annotations=READ)
    async def get_strategy_agreement(portfolio_id: PortfolioId = None) -> dict[str, Any]:
        """For each holding of one of your portfolios: whether each active
        strategy's latest signal agrees or disagrees with it, and why."""
        return await t.get("/api/insights/agreement", {"portfolio_id": portfolio_id})
