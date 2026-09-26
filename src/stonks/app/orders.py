"""OrdersService — orders and fills recorded by production ticks."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from stonks.app.context import AppContext
from stonks.app.pagination import Page


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
    broker_order_id: str | None
    created_at: str
    updated_at: str


class FillView(BaseModel):
    id: int
    order_client_id: str
    tick_id: str | None
    ticker: str
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
    ) -> Page[OrderView]:
        clause, params = _where(
            {"tick_id": tick_id, "strategy_id": strategy_id, "ticker": ticker, "status": status}
        )
        with self._ctx.state() as state:
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
    ) -> Page[FillView]:
        clause, params = _where(
            {"o.tick_id": tick_id, "f.ticker": ticker, "f.order_client_id": order_client_id}
        )
        base = f"FROM fills f LEFT JOIN orders o ON o.client_id = f.order_client_id{clause}"
        with self._ctx.state() as state:
            total = int(state.sql(f"SELECT COUNT(*) {base}", params)[0][0])
            rows = state.sql(
                f"SELECT f.*, o.tick_id AS tick_id {base} "
                "ORDER BY f.filled_at DESC, f.id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            )
        items = [FillView(**dict(r)) for r in rows]
        return Page[FillView](items=items, total=total, limit=limit, offset=offset)


def _where(filters: dict[str, Any]) -> tuple[str, list[Any]]:
    """``WHERE`` clause over fixed (code-defined) column names."""
    parts = [f"{col} = ?" for col, v in filters.items() if v is not None]
    params = [v for v in filters.values() if v is not None]
    return (f" WHERE {' AND '.join(parts)}" if parts else ""), params
