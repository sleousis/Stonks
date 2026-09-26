"""Portfolio and subscription tools (roadmap step S7): read your portfolios,
whether each trades paper or live, and your subscriptions; subscribe and
change a subscription behind ``confirm``. Switching to ``auto`` needs a
fresh second factor, so it is refused here with a pointer to the web app
(design section 8)."""

# No ``from __future__ import annotations`` (see common.py).

from typing import Annotated, Any, Literal

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from stonks.mcp.guards import CONFIRM_HINT
from stonks.mcp.tools.common import READ, Confirm, ToolContext, drop_none, items, seg

#: Changes which strategies trade or signal for your portfolios.
SUBSCRIPTION_WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
)

StartMode = Annotated[
    Literal["notify", "paper"],
    Field(description="notify: signals only; paper: simulated orders on one of your portfolios"),
]
NewMode = Annotated[
    Literal["notify", "paper", "auto"] | None,
    Field(description="notify or paper; auto is switched on in the web app (second factor)"),
]
AUTO_REFUSAL = (
    "switching a subscription to auto needs a fresh second factor: do it in the web app "
    "(Strategies > your subscription > Auto)"
)


def register(t: ToolContext) -> None:
    server = t.server

    @server.tool(annotations=READ)
    async def list_portfolios() -> dict[str, Any]:
        """Your portfolios, oldest first, each marked paper or live."""
        return items(await t.get("/api/portfolios"))

    @server.tool(annotations=READ)
    async def list_trading_modes() -> dict[str, Any]:
        """For each of your portfolios: paper or live money, and the broker
        (simulated ledger, the configured Alpaca account, or a linked
        connection)."""
        return items(await t.get("/api/portfolios/trading-modes"))

    @server.tool(annotations=READ)
    async def list_subscriptions() -> dict[str, Any]:
        """Your subscriptions: strategy, portfolio, mode (notify, paper,
        auto), whether it is on, the paper-day count and what still blocks
        auto."""
        return items(await t.get("/api/subscriptions"))

    @server.tool(annotations=SUBSCRIPTION_WRITE)
    async def subscribe(
        strategy_id: str,
        mode: StartMode = "notify",
        portfolio_id: Annotated[
            str | None, Field(description="one of your portfolios (needed for paper)")
        ] = None,
        confirm: Confirm = False,
    ) -> dict[str, Any]:
        """Follow a strategy in notify or paper mode. Without confirm=true
        returns a preview and changes nothing."""
        body = drop_none({"strategy_id": strategy_id, "mode": mode, "portfolio_id": portfolio_id})
        if not confirm:
            target = f"portfolio {portfolio_id}" if portfolio_id else "no portfolio (signals only)"
            return {
                "preview": True,
                "applied": False,
                "request": body,
                "warnings": [f"{strategy_id} starts in {mode} mode for {target}"],
                "next_step": CONFIRM_HINT,
            }
        sub = await t.post("/api/subscriptions", body)
        return {"preview": False, "applied": True, "subscription": sub}

    @server.tool(annotations=SUBSCRIPTION_WRITE)
    async def update_subscription(
        subscription_id: str,
        enabled: bool | None = None,
        mode: NewMode = None,
        reason: Annotated[str | None, Field(max_length=500, description="audited")] = None,
        confirm: Confirm = False,
    ) -> dict[str, Any]:
        """Turn one of your subscriptions on or off, or move it between
        notify and paper. Auto is refused here (it needs the web app).
        Without confirm=true returns a preview and changes nothing."""
        if mode == "auto":
            raise ToolError(AUTO_REFUSAL)
        body = drop_none({"enabled": enabled, "mode": mode, "reason": reason})
        if not body or set(body) == {"reason"}:
            raise ToolError("give enabled and/or mode")
        sid = seg(subscription_id)
        if not confirm:
            current = next(
                (s for s in await t.get("/api/subscriptions") if s.get("id") == subscription_id),
                None,
            )
            if current is None:
                raise ToolError(f"subscription {subscription_id!r} not found")
            return {
                "preview": True,
                "applied": False,
                "subscription": current,
                "request": body,
                "next_step": CONFIRM_HINT,
            }
        sub = await t.patch(f"/api/subscriptions/{sid}", body)
        return {"preview": False, "applied": True, "subscription": sub}
