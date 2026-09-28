"""Live trading reads (roadmap 19.9): a live portfolio's stage, its gate
report, the allocation, the live rules and the IB Gateway health.

Read only. Promoting a stage and changing the allocation or the account
profile need a fresh second factor, so they stay in the console (and the
operator's shell). The dry-run preview runs a tick through the broker, so
it stays in the console and the CLI too."""

# No ``from __future__ import annotations`` (see common.py).

from typing import Annotated, Any

from pydantic import Field

from stonks.mcp.tools.common import READ, ToolContext, seg

LivePortfolio = Annotated[str, Field(description="a portfolio id of yours (pf_...)")]


def register(t: ToolContext) -> None:
    server = t.server

    @server.tool(annotations=READ)
    async def get_live_stage(portfolio_id: LivePortfolio, days: int = 30) -> dict[str, Any]:
        """A portfolio's live stage (sim_paper, broker_paper, live_small,
        live_scale), its stage changes, and the last sessions' gate metrics:
        orders sent, rejected and stuck, fills with no commission, the TCA
        gap, the book's and the model book's returns, and drift."""
        return await t.get(
            f"/api/portfolios/{seg(portfolio_id)}/live/stage",
            {"days": max(1, min(int(days), 260))},
        )

    @server.tool(annotations=READ)
    async def get_live_gate_report(portfolio_id: LivePortfolio) -> dict[str, Any]:
        """What a promotion to the next stage needs, checked now. A check
        with passed = null has no data yet and does not block. Promoting is
        done in the web app with a fresh second factor, never here."""
        return await t.get(f"/api/portfolios/{seg(portfolio_id)}/live/gate-report")

    @server.tool(annotations=READ)
    async def get_live_allocation(portfolio_id: LivePortfolio) -> dict[str, Any]:
        """How much Stonks may trade in a live portfolio, set by hand by its
        owner (amount null: not set, so nothing opens)."""
        return await t.get(f"/api/portfolios/{seg(portfolio_id)}/live/allocation")

    @server.tool(annotations=READ)
    async def get_live_rules(portfolio_id: LivePortfolio) -> dict[str, Any]:
        """Which live safeguards and account rules act on a live portfolio,
        with their settings as its book follows them."""
        return await t.get(f"/api/portfolios/{seg(portfolio_id)}/live/rules")

    @server.tool(annotations=READ)
    async def get_broker_gateways() -> dict[str, Any]:
        """IB Gateway health: connected or not, the last good check, and the
        auto books of yours a gateway outage paused."""
        return await t.get("/api/brokers/gateways")
