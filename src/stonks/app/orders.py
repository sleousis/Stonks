"""OrdersService — orders and fills recorded by production ticks."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from stonks.app.context import AppContext
from stonks.app.pagination import Page
from stonks.production.ledger import ledger_filter


class OrderView(BaseModel):
    client_id: str
    tick_id: str | None
    strategy_id: str | None
    ticker: str
    side: str
    quantity: float
    order_type: str
    limit_price: float | None
    status: str
    #: Why the order ended in its status (e.g. the broker's rejection
    #: message); None when there is nothing to explain.
    status_reason: str | None = None
    broker_order_id: str | None
    created_at: str
    updated_at: str


class FillView(BaseModel):
    id: int
    order_client_id: str
    tick_id: str | None
    ticker: str
    #: The order's side (``buy`` or ``sell``); null when the order is gone.
    side: str | None = None
    quantity: float
    price: float
    fee: float
    filled_at: str


class OrdersService:
    def __init__(self, context: AppContext) -> None:
        self._ctx = context

    def orders(
        self,
        *,
        tick_id: str | None = None,
        strategy_id: str | None = None,
        ticker: str | None = None,
        status: str | None = None,
        limit: int,
        offset: int,
        portfolio_id: str,
    ) -> Page[OrderView]:
        """One portfolio's orders. The caller names the portfolio (the
        route resolves one the caller owns), never a default (BE-46)."""
        clause, params = _where(
            {"tick_id": tick_id, "strategy_id": strategy_id, "ticker": ticker, "status": status}
        )
        with self._ctx.state() as state:
            clause, params = _scoped(state, "orders", portfolio_id, None, clause, params)
            total = int(state.sql(f"SELECT COUNT(*) FROM orders{clause}", params)[0][0])
            rows = state.sql(
                f"SELECT * FROM orders{clause} ORDER BY created_at DESC, rowid DESC "
                "LIMIT ? OFFSET ?",
                [*params, limit, offset],
            )
        items = [OrderView(**dict(r)) for r in rows]
        return Page[OrderView](items=items, total=total, limit=limit, offset=offset)

    def fills(
        self,
        *,
        tick_id: str | None = None,
        ticker: str | None = None,
        order_client_id: str | None = None,
        limit: int,
        offset: int,
        portfolio_id: str,
    ) -> Page[FillView]:
        """One portfolio's fills. The caller names the portfolio (BE-46)."""
        clause, params = _where(
            {"o.tick_id": tick_id, "f.ticker": ticker, "f.order_client_id": order_client_id}
        )
        with self._ctx.state() as state:
            clause, params = _scoped(state, "fills", portfolio_id, "f", clause, params)
            base = f"FROM fills f LEFT JOIN orders o ON o.client_id = f.order_client_id{clause}"
            total = int(state.sql(f"SELECT COUNT(*) {base}", params)[0][0])
            rows = state.sql(
                f"SELECT f.*, o.tick_id AS tick_id, o.side AS side {base} "
                "ORDER BY f.filled_at DESC, f.id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            )
        items = [FillView(**dict(r)) for r in rows]
        return Page[FillView](items=items, total=total, limit=limit, offset=offset)


def _scoped(
    state: Any, table: Any, portfolio_id: str, alias: str | None, clause: str, params: list[Any]
) -> tuple[str, list[Any]]:
    """``clause`` narrowed to one portfolio's rows."""
    where, extra = ledger_filter(state, table, portfolio_id, alias=alias)
    joiner = " AND " if clause else " WHERE "
    return f"{clause}{joiner}{where}", [*params, *extra]


def _where(filters: dict[str, Any]) -> tuple[str, list[Any]]:
    """``WHERE`` clause over fixed (code-defined) column names."""
    parts = [f"{col} = ?" for col, v in filters.items() if v is not None]
    params = [v for v in filters.values() if v is not None]
    return (f" WHERE {' AND '.join(parts)}" if parts else ""), params
