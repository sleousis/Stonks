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
    async def get_strategy_agreement(portfolio_id: PortfolioId = None) -> dict[str, Any]:
        """For each holding of one of your portfolios: whether each active
        strategy's latest signal agrees or disagrees with it, and why."""
        return await t.get("/api/insights/agreement", {"portfolio_id": portfolio_id})
