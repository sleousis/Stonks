"""Manual order tools (roadmap 20.1): place, change and cancel your own
orders. Each needs ``confirm=true``. Without it, ``place_order`` and
``change_order`` return the API's preview (every check, nothing placed) and
``cancel_order`` returns the order. A book that trades real money needs a
fresh second factor, which a token never has: those orders are placed in
the web app."""

# No ``from __future__ import annotations`` (see common.py).

from typing import Annotated, Any, Literal

from mcp.types import ToolAnnotations
from pydantic import Field

from stonks.mcp.guards import CONFIRM_HINT
from stonks.mcp.tools.common import (
    READ,
    WRITE,
    Confirm,
    PortfolioId,
    Ticker,
    ToolContext,
    drop_none,
    items,
    seg,
)

#: Places or cancels an order: destructive, and a new client id each time.
ORDER = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
)

OrderReason = Annotated[
    str, Field(min_length=3, max_length=500, description="why you trade (recorded and audited)")
]
ClientKey = Annotated[
    str | None,
    Field(
        pattern=r"^[A-Za-z0-9_.\-]{1,64}$",
        description="your idempotency key: the same key places the order once",
    ),
]
DRAFT_HINTS = {409: "The draft was refused by a check (price band, caps or an unknown ticker)"}
ORDER_HINTS = {
    409: "The order was refused by a check (the kill switch, a halt or a risk rule)",
}


def register(t: ToolContext) -> None:
    server = t.server

    @server.tool(annotations=ORDER)
    async def place_order(
        ticker: Ticker,
        side: Literal["buy", "sell"],
        quantity: Annotated[float, Field(gt=0)],
        reason: OrderReason,
        portfolio_id: PortfolioId = None,
        order_type: Literal["market", "limit"] = "market",
        limit_price: Annotated[float | None, Field(gt=0)] = None,
        client_id: ClientKey = None,
        allow_reduce: Annotated[
            bool, Field(description="accept a smaller order when a risk rule shrinks it")
        ] = False,
        confirm: Confirm = False,
    ) -> dict[str, Any]:
        """Place a manual order on one of your portfolios. It goes through the
        kill switch, every halt and every risk rule, like a strategy's order.
        Without confirm=true returns the preview (what would be placed, the
        risk rules' adjustments) and places nothing. Real-money books are
        refused here: place those in the web app with a fresh second factor."""
        body = drop_none(
            {
                "portfolio_id": portfolio_id,
                "ticker": ticker,
                "side": side,
                "quantity": quantity,
                "order_type": order_type,
                "limit_price": limit_price,
                "reason": reason,
                "client_id": client_id,
                "allow_reduce": allow_reduce,
            }
        )
        if not confirm:
            preview = await t.post("/api/orders/manual/preview", body, hints=ORDER_HINTS)
            warnings = []
            if preview.get("live"):
                warnings.append(
                    "this book trades real money: the order must be placed in the web app "
                    "with a fresh second factor"
                )
            if preview.get("adjustments"):
                warnings.append("the risk rules change this order; see adjustments")
            return {
                "preview": True,
                "applied": False,
                "order": preview,
                "warnings": warnings,
                "next_step": CONFIRM_HINT,
            }
        placed = await t.post("/api/orders/manual", body, hints=ORDER_HINTS)
        return {"preview": False, "applied": True, "order": placed}

    @server.tool(annotations=ORDER)
    async def change_order(
        client_id: str,
        reason: OrderReason,
        portfolio_id: PortfolioId = None,
        quantity: Annotated[float | None, Field(gt=0)] = None,
        limit_price: Annotated[float | None, Field(gt=0)] = None,
        allow_reduce: bool = False,
        confirm: Confirm = False,
    ) -> dict[str, Any]:
        """Change the quantity or limit of one of your working manual orders.
        The order is cancelled at the broker and a new one replaces it, after
        every check again. Without confirm=true returns the order as it is
        now and changes nothing."""
        cid = seg(client_id)
        body = drop_none(
            {
                "portfolio_id": portfolio_id,
                "quantity": quantity,
                "limit_price": limit_price,
                "reason": reason,
                "allow_reduce": allow_reduce,
            }
        )
        if not confirm:
            current = await _find(t, cid, portfolio_id)
            return {
                "preview": True,
                "applied": False,
                "order": current,
                "request": body,
                "warnings": ["the order is cancelled and replaced by a new order"],
                "next_step": CONFIRM_HINT,
            }
        changed = await t.post(f"/api/orders/{cid}/change", body, hints=ORDER_HINTS)
        return {"preview": False, "applied": True, "order": changed}

    @server.tool(annotations=ORDER)
    async def cancel_order(
        client_id: str,
        reason: OrderReason,
        portfolio_id: PortfolioId = None,
        confirm: Confirm = False,
    ) -> dict[str, Any]:
        """Cancel one working order of your portfolio (yours or a strategy's).
        Without confirm=true returns the order and cancels nothing."""
        cid = seg(client_id)
        body = drop_none({"portfolio_id": portfolio_id, "reason": reason})
        if not confirm:
            current = await _find(t, cid, portfolio_id)
            return {
                "preview": True,
                "applied": False,
                "order": current,
                "next_step": CONFIRM_HINT,
            }
        cancelled = await t.post(f"/api/orders/{cid}/cancel", body, hints=ORDER_HINTS)
        return {"preview": False, "applied": True, "result": cancelled}

    @server.tool(annotations=WRITE)
    async def draft_order(
        ticker: Ticker,
        side: Literal["buy", "sell"],
        quantity: Annotated[float, Field(gt=0)],
        reason: OrderReason,
        retry_key: Annotated[
            str,
            Field(
                pattern=r"^[A-Za-z0-9_.:\-]{1,120}$",
                description="the same key returns the draft already made (a safe retry)",
            ),
        ],
        portfolio_id: PortfolioId = None,
        order_type: Literal["market", "limit"] = "market",
        limit_price: Annotated[float | None, Field(gt=0)] = None,
    ) -> dict[str, Any]:
        """Propose an order without placing it. The server prices it at the
        latest close and checks it. The person approves it in the web app
        with a fresh second factor, and only then is it placed."""
        body = drop_none(
            {
                "portfolio_id": portfolio_id,
                "ticker": ticker,
                "side": side,
                "quantity": quantity,
                "order_type": order_type,
                "limit_price": limit_price,
                "reason": reason,
                "retry_key": retry_key,
            }
        )
        return await t.post("/api/orders/drafts", body, hints=DRAFT_HINTS)

    @server.tool(annotations=READ)
    async def list_order_drafts(
        status: Literal["pending", "placed", "rejected", "expired", "cancelled"] | None = None,
    ) -> dict[str, Any]:
        """Your order drafts, newest first: proposed orders waiting for your
        approval in the web app, and what became of the others."""
        return items(await t.get("/api/orders/drafts", drop_none({"status": status, "limit": 200})))


async def _find(t: ToolContext, client_id: str, portfolio_id: str | None) -> Any:
    """The order with ``client_id`` in the portfolio, or ``None``."""
    page = await t.get("/api/orders", drop_none({"portfolio_id": portfolio_id, "limit": 500}))
    for item in page.get("items", []):
        if item.get("client_id") == client_id:
            return item
    return None
