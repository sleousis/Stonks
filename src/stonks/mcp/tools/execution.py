"""Execution algos and the rebalancing planner (roadmap 23.16). Reading
the algo catalog, a portfolio's settings, its parent orders and a plan is
free. Setting an algo and confirming a plan need ``confirm=true``. A
confirmed plan only writes tickets that wait for approval, which needs a
fresh second factor in the web app: nothing is sent from here."""

# No ``from __future__ import annotations`` (see common.py).

from typing import Annotated, Any, Literal

from pydantic import Field

from stonks.mcp.guards import CONFIRM_HINT
from stonks.mcp.tools.common import (
    READ,
    STATUS_CHANGE,
    Confirm,
    ToolContext,
    drop_none,
    seg,
)

PLAN_HINTS = {422: "The plan was refused: check the weights (long only, at most 1 in all)"}
PlanSource = Literal["strategy", "targets"]
Weights = Annotated[
    dict[str, float] | None,
    Field(description='your own targets, ticker to weight, e.g. {"AAPL.US": 0.3}'),
]
Rate = Annotated[float | None, Field(ge=0, le=1, description="tax rate as a fraction")]


def _plan_body(
    portfolio_id: str,
    source: PlanSource,
    strategy_id: str | None,
    targets: dict[str, float] | None,
    min_trade_value: float,
    short_term_rate: float | None,
    long_term_rate: float | None,
) -> dict[str, Any]:
    return drop_none(
        {
            "portfolio_id": portfolio_id,
            "source": source,
            "strategy_id": strategy_id,
            "targets": [{"ticker": k, "weight": v} for k, v in (targets or {}).items()] or None,
            "min_trade_value": min_trade_value,
            "short_term_rate": short_term_rate,
            "long_term_rate": long_term_rate,
        }
    )


def register(t: ToolContext) -> None:
    server = t.server

    @server.tool(annotations=READ)
    async def list_execution_algos() -> dict[str, Any]:
        """Every execution algo (Adaptive, TWAP, VWAP): its parameters,
        defaults, whether Stonks can slice it at brokers without it, and the
        costs the backtest assumes for it."""
        return await t.get("/api/execution/algos")

    @server.tool(annotations=READ)
    async def get_execution_algo_settings(portfolio_id: str) -> dict[str, Any]:
        """How one of your portfolio's orders are worked: its own algo and
        each strategy's override. Empty: plain limit orders."""
        return await t.get(f"/api/portfolios/{seg(portfolio_id)}/execution-algos")

    @server.tool(annotations=STATUS_CHANGE)
    async def set_execution_algo(
        portfolio_id: str,
        algo: Literal["adaptive", "twap", "vwap"],
        params: Annotated[dict[str, Any] | None, Field(description="the algo's parameters")] = None,
        strategy_id: Annotated[str | None, Field(description="only this strategy's orders")] = None,
        confirm: Confirm = False,
    ) -> dict[str, Any]:
        """Work a portfolio's orders (or one strategy's) with an algo from the
        next tickets on. Without confirm=true returns the current settings and
        changes nothing."""
        pid = seg(portfolio_id)
        body = drop_none({"algo": algo, "params": params or {}, "strategy_id": strategy_id})
        if not confirm:
            current = await t.get(f"/api/portfolios/{pid}/execution-algos")
            return {"preview": True, "applied": False, "current": current, "change": body,
                    "next_step": CONFIRM_HINT}  # fmt: skip
        done = await t.put(f"/api/portfolios/{pid}/execution-algos", body)
        return {"preview": False, "applied": True, "setting": done}

    @server.tool(annotations=READ)
    async def list_algo_parents(portfolio_id: str) -> dict[str, Any]:
        """Parent orders Stonks works as child slices (TWAP, VWAP at a broker
        without them), with each slice's state."""
        return await t.get(f"/api/portfolios/{seg(portfolio_id)}/algo-parents")

    @server.tool(annotations=READ)
    async def plan_rebalance(
        portfolio_id: str,
        source: PlanSource,
        strategy_id: str | None = None,
        targets: Weights = None,
        min_trade_value: Annotated[float, Field(ge=0)] = 0.0,
        short_term_rate: Rate = None,
        long_term_rate: Rate = None,
    ) -> dict[str, Any]:
        """The trades that move one of your portfolios to a strategy's model
        weights (source=strategy) or your own targets (source=targets): whole
        shares, costs, turnover and a tax preview. Writes and sends nothing."""
        body = _plan_body(portfolio_id, source, strategy_id, targets, min_trade_value,
                          short_term_rate, long_term_rate)  # fmt: skip
        return await t.post("/api/planner/plan", body, hints=PLAN_HINTS)

    @server.tool(annotations=STATUS_CHANGE)
    async def confirm_rebalance(
        portfolio_id: str,
        source: PlanSource,
        reason: Annotated[str, Field(min_length=3, max_length=500, description="why (audited)")],
        strategy_id: str | None = None,
        targets: Weights = None,
        min_trade_value: Annotated[float, Field(ge=0)] = 0.0,
        confirm: Confirm = False,
    ) -> dict[str, Any]:
        """Write one order ticket per trade of the plan. Each waits for the
        person's approval with a fresh second factor in the web app, so
        nothing is sent from here. Without confirm=true returns the plan."""
        body = _plan_body(portfolio_id, source, strategy_id, targets, min_trade_value, None, None)
        if not confirm:
            plan = await t.post("/api/planner/plan", body, hints=PLAN_HINTS)
            return {"preview": True, "applied": False, "plan": plan, "next_step": CONFIRM_HINT}
        done = await t.post("/api/planner/confirm", {**body, "reason": reason}, hints=PLAN_HINTS)
        return {
            "preview": False,
            "applied": True,
            "result": done,
            "next_step": "Approve the tickets in the web app (Approvals) with a fresh second factor.",
        }
