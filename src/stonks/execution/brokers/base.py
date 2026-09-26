"""Broker-side types shared by every broker adapter.

These are *our* types: adapters translate vendor objects into them so no
vendor SDK type ever crosses the ``execution.brokers`` seam.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from stonks.core.types import OrderSide, OrderStatus


class BrokerError(RuntimeError):
    """A broker call failed after retries (or failed non-retryably)."""


class UnsupportedTickerError(ValueError):
    """The ticker cannot be traded through this broker (e.g. non-US listing)."""


class LiveTradingRefusedError(RuntimeError):
    """A live (real-money) endpoint was requested without ``allow_live=True``."""


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


@runtime_checkable
class OrderStateSource(Protocol):
    """Optional broker capability: look up an order by our client_id.

    Brokers with a persistent order book (Alpaca) implement it so a fresh,
    stateless process can reconcile orders placed by an earlier tick.
    Returns ``None`` when the broker has never seen the client_id.
    """

    def get_order_state(self, client_id: str) -> BrokerOrderState | None: ...
