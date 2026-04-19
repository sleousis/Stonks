"""Order-id helpers shared by backtest and production.

``make_client_id`` assembles a deterministic, idempotency-friendly identifier
from the (tick, strategy, ticker, side) tuple. Any broker honoring the Broker
Protocol must short-circuit duplicate submissions of the same client_id.
"""

from __future__ import annotations

from stonks.core.types import OrderSide


def make_client_id(
    *,
    tick_id: str,
    strategy_id: str,
    ticker: str,
    side: OrderSide,
) -> str:
    return f"{tick_id}:{strategy_id}:{ticker}:{side}"
