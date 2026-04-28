"""Cross-block value types.

Kept minimal and boundary-independent so that ``ingest``, ``backtest``,
``registry``, ``execution``, and ``production`` all agree on what an Order,
a Fill, and a Portfolio are.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

OrderSide = Literal["buy", "sell"]
OrderType = Literal["market", "limit", "stop", "stop_limit"]
OrderStatus = Literal["pending", "filled", "partially_filled", "rejected", "cancelled"]

# Top-level asset class. Closed set; future additions (forex, fund, index)
# are non-breaking. Lives in core so every block (ingest, lake, strategies,
# ranker) imports from the same place.
AssetClass = Literal["equity", "crypto", "commodity", "bond"]


@dataclass(frozen=True)
class Order:
    client_id: str
    ticker: str
    side: OrderSide
    quantity: float
    order_type: OrderType = "market"
    limit_price: float | None = None
    strategy_id: str | None = None
    tick_id: str | None = None

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"Order.quantity must be positive, got {self.quantity}")
        if self.order_type in ("limit", "stop_limit") and self.limit_price is None:
            raise ValueError(f"Order.limit_price is required for order_type={self.order_type!r}")


@dataclass(frozen=True)
class Fill:
    order_client_id: str
    ticker: str
    quantity: float
    price: float
    fee: float
    filled_at: datetime
    side: OrderSide

    @property
    def signed_quantity(self) -> float:
        return self.quantity if self.side == "buy" else -self.quantity


@dataclass
class Portfolio:
    cash: float
    positions: dict[str, float] = field(default_factory=dict)

    def apply_fill(self, fill: Fill) -> None:
        current = self.positions.get(fill.ticker, 0.0)
        new = current + fill.signed_quantity
        if abs(new) < 1e-12:
            self.positions.pop(fill.ticker, None)
        else:
            self.positions[fill.ticker] = new
        cash_delta = -fill.signed_quantity * fill.price - fill.fee
        self.cash += cash_delta

    def total_value(self, prices: Mapping[str, float]) -> float:
        mark = sum(qty * prices.get(t, 0.0) for t, qty in self.positions.items())
        return self.cash + mark


@dataclass(frozen=True)
class Features:
    values: Mapping[str, float] = field(default_factory=dict)

    def get(self, name: str, default: Any = None) -> Any:
        return self.values.get(name, default)

    def __len__(self) -> int:
        return len(self.values)

    def __iter__(self):
        return iter(self.values)
