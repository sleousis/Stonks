"""Round-trip journal tools (roadmap 23.3): reads only. Reviewing trades and
editing playbooks happen in the console or the CLI."""

# No ``from __future__ import annotations`` (see common.py).

from typing import Annotated, Any, Literal

from pydantic import Field

from stonks.mcp.tools.common import READ, ToolContext, drop_none

Day = Annotated[str | None, Field(description="YYYY-MM-DD")]
Origin = Annotated[
    Literal["strategy", "manual"] | None, Field(description="strategy or manual orders")
]
By = Annotated[
    Literal[
        "all",
        "sleeve",
        "origin",
        "ticker",
        "side",
        "exit_trigger",
        "tag",
        "mistake",
        "playbook",
        "plan",
    ],
    Field(description="how to group the trades"),
]


def register(t: ToolContext) -> None:
    server = t.server

    @server.tool(annotations=READ)
    async def list_round_trips(
        status: Annotated[
            Literal["all", "open", "closed"], Field(description="all, open or closed")
        ] = "all",
        sleeve: Annotated[str | None, Field(description="one strategy id, or manual")] = None,
        origin: Origin = None,
        ticker: str | None = None,
        since: Day = None,
        until: Day = None,
        tag: str | None = None,
        mistake: str | None = None,
        plan: Annotated[
            Literal["followed", "broke", "not_said"] | None,
            Field(description="whether the plan was followed"),
        ] = None,
        portfolio_id: str | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> dict[str, Any]:
        """Your round trips built from fills, open first then newest exit
        first: entry and exit, holding days, P&L, adverse and favourable
        excursion, R multiple (when a stop is known), exit efficiency, and
        your tags, mistakes and playbook."""
        query = drop_none(
            {
                "status": status,
                "sleeve": sleeve,
                "origin": origin,
                "ticker": ticker,
                "since": since,
                "until": until,
                "tag": tag,
                "mistake": mistake,
                "plan": plan,
                "portfolio_id": portfolio_id,
                "limit": limit,
                "offset": offset,
            }
        )
        return await t.get("/api/journal/trades", params=query)

    @server.tool(annotations=READ)
    async def get_round_trip(
        trade_id: Annotated[int, Field(ge=1, description="the id of the opening fill")],
        portfolio_id: str | None = None,
    ) -> dict[str, Any]:
        """One trade with every leg (each partial exit) and its review."""
        return await t.get(
            f"/api/journal/trades/{trade_id}", params=drop_none({"portfolio_id": portfolio_id})
        )

    @server.tool(annotations=READ)
    async def get_pnl_calendar(
        since: Day = None,
        until: Day = None,
        sleeve: str | None = None,
        origin: Origin = None,
        portfolio_id: str | None = None,
    ) -> dict[str, Any]:
        """Realised P&L of closed trades by exit day, with weekly and
        monthly totals, in the portfolio's base currency."""
        query = drop_none(
            {
                "since": since,
                "until": until,
                "sleeve": sleeve,
                "origin": origin,
                "portfolio_id": portfolio_id,
            }
        )
        return await t.get("/api/journal/calendar", params=query)

    @server.tool(annotations=READ)
    async def get_journal_breakdown(
        by: By = "all",
        since: Day = None,
        until: Day = None,
        sleeve: str | None = None,
        origin: Origin = None,
        portfolio_id: str | None = None,
    ) -> dict[str, Any]:
        """Win rate, P&L, profit factor, average R and exit efficiency per
        group: by sleeve, tag, mistake, playbook, or followed versus broke
        plan."""
        query = drop_none(
            {
                "by": by,
                "since": since,
                "until": until,
                "sleeve": sleeve,
                "origin": origin,
                "portfolio_id": portfolio_id,
            }
        )
        return await t.get("/api/journal/breakdown", params=query)

    @server.tool(annotations=READ)
    async def list_playbooks(include_archived: bool = False) -> Any:
        """Your playbooks: the setups you trade, with their rules."""
        return await t.get("/api/journal/playbooks", params={"include_archived": include_archived})
