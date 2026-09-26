"""Broker-side types shared by every broker adapter.

These are *our* types: adapters translate vendor objects into them so no
vendor SDK type ever crosses the ``execution.brokers`` seam.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from stonks.core.types import Fill, OrderSide, OrderStatus

#: Which broker the production tick trades through (``[brokers].kind``).
BrokerKind = Literal["simulated", "alpaca"]


class BrokerError(RuntimeError):
    """A broker call failed after retries (or failed non-retryably)."""


class UnsupportedTickerError(ValueError):
    """The ticker cannot be traded through this broker (e.g. non-US listing)."""


class LiveTradingRefusedError(RuntimeError):
    """A live (real-money) endpoint was requested without ``allow_live=True``."""


class OrderRejectedError(BrokerError):
    """A pre-trade check refused the order before it was sent (blocked
    account, untradable asset, quantity below the broker minimum, ...)."""


@dataclass(frozen=True)
class BrokerAccount:
    """Account-level balances and trading flags, broker-agnostic."""

    cash: float
    equity: float
    buying_power: float
    currency: str
    status: str
    trading_blocked: bool = False
    pattern_day_trader: bool = False

    @property
    def can_trade(self) -> bool:
        return self.status.upper() == "ACTIVE" and not self.trading_blocked


@dataclass(frozen=True)
class MarketClock:
    """Exchange session state; all timestamps are timezone-aware UTC."""

    timestamp: datetime
    is_open: bool
    next_open: datetime
    next_close: datetime


@dataclass(frozen=True)
class BrokerOrderState:
    """The broker's current view of one order, keyed by our ``client_id``.

    ``filled_quantity`` and ``avg_fill_price`` are *cumulative* over the
    order's life, which lets reconciliation derive the not-yet-recorded fill
    delta from what is already in the ``fills`` table, idempotently.
    """

    client_id: str
    broker_order_id: str
    ticker: str
    side: OrderSide
    status: OrderStatus
    quantity: float
    filled_quantity: float
    avg_fill_price: float | None
    updated_at: datetime | None = None


QTY_EPSILON = 1e-9


def delta_fill(
    state: BrokerOrderState,
    *,
    recorded_quantity: float,
    recorded_notional: float,
    filled_at: datetime,
    fee: float = 0.0,
) -> Fill | None:
    """The part of ``state``'s cumulative fill not yet recorded, as a Fill.

    ``recorded_quantity`` / ``recorded_notional`` (sum of qty*price) describe
    what has already been booked for this order. The delta's price is backed
    out of the cumulative average so booked + delta reproduces the broker's
    ``filled_quantity * avg_fill_price`` exactly. Returns ``None`` when there
    is nothing new (or the broker reports less than was booked).
    """
    delta_qty = state.filled_quantity - recorded_quantity
    if delta_qty <= QTY_EPSILON or state.avg_fill_price is None:
        return None
    total_notional = state.filled_quantity * state.avg_fill_price
    price = (total_notional - recorded_notional) / delta_qty
    return Fill(
        order_client_id=state.client_id,
        ticker=state.ticker,
        quantity=delta_qty,
        price=price,
        fee=fee,
        filled_at=filled_at,
        side=state.side,
    )


@runtime_checkable
class OrderStateSource(Protocol):
    """Optional broker capability: look up an order by our client_id.

    Brokers with a persistent order book (Alpaca) implement it so a fresh,
    stateless process can reconcile orders placed by an earlier tick.
    Returns ``None`` when the broker has never seen the client_id.
    """

    def get_order_state(self, client_id: str) -> BrokerOrderState | None: ...
