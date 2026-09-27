"""Research loop tools (roadmap 22.9): start an AI research session and
read its proposals. A session only runs lab trials, each counted in the
trial ledger, and never registers or promotes a strategy."""

# No ``from __future__ import annotations`` (see common.py).

from typing import Annotated, Any

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from stonks.mcp.tools.common import JOB, READ, Limit, Offset, ToolContext, drop_none, items, seg

SessionId = Annotated[str, Field(min_length=1, max_length=64, description="the rs_... id")]


def register(t: ToolContext) -> None:
    server = t.server

    @server.tool(annotations=JOB)
    async def start_research(
        goal: Annotated[
            str, Field(min_length=10, max_length=2000, description="what to look for")
        ],
        universe: Annotated[
            list[str] | None, Field(description="instrument ids, or give universe_id")
        ] = None,
        universe_id: Annotated[
            str | None,
            Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,63}$", description="a stored universe"),
        ] = None,
        max_trials: Annotated[
            int | None, Field(ge=1, description="tuning trials, at most the setting")
        ] = None,
        max_proposals: Annotated[
            int | None, Field(ge=1, description="proposals, at most the setting")
        ] = None,
        max_cpu_seconds: Annotated[
            float | None, Field(gt=0, description="compute seconds, at most the setting")
        ] = None,
    ) -> dict[str, Any]:
        """Start an AI research session: the assistant's model proposes lab
        trials for the goal and runs them under the session's budgets. Every
        proposal is recorded with its hypothesis first, every trial is counted
        in the trial ledger, and validation windows start after the model's
        training cutoff. It never registers or promotes a strategy. Returns
        the job; use wait_for_job, then get_research_session."""
        if not universe and not universe_id:
            raise ToolError("give universe (tickers) or universe_id")
        body = drop_none(
            {
                "goal": goal,
                "universe": universe,
                "universe_id": universe_id,
                "max_trials": max_trials,
                "max_proposals": max_proposals,
                "max_cpu_seconds": max_cpu_seconds,
            }
        )
        return await t.post("/api/assistant/research", body)

    @server.tool(annotations=READ)
    async def list_research_sessions(limit: Limit = 50, offset: Offset = 0) -> dict[str, Any]:
        """Your AI research sessions, newest first: goal, budgets used, status."""
        return items(await t.get("/api/assistant/research", {"limit": limit, "offset": offset}))

    @server.tool(annotations=READ)
    async def get_research_session(session_id: SessionId) -> dict[str, Any]:
        """One research session with every proposal: its hypothesis, whether
        it ran or why not, and its lab run in the trial ledger."""
        return await t.get(f"/api/assistant/research/{seg(session_id)}")
