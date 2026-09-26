"""Order-id helpers shared by backtest and production.

``make_client_id`` assembles a deterministic, idempotency-friendly identifier
from the (as_of date, strategy, ticker, side) tuple. It deliberately does not
include the per-run ``tick_id``: a crashed tick re-run for the same ``as_of``
must reproduce the same client_ids so already-submitted orders can be
recognized and skipped. Any broker honoring the Broker Protocol must
short-circuit duplicate submissions of the same client_id.
"""

from __future__ import annotations

from datetime import date

from stonks.core.types import OrderSide


def make_client_id(
    *,
    as_of: date,
    strategy_id: str,
    ticker: str,
    side: OrderSide,
) -> str:
    """Return ``"<as_of ISO date>:<strategy_id>:<ticker>:<side>"``.

    Stable across re-runs of the same trading day, distinct across days,
    strategies, tickers and sides.
    """
    return f"{as_of.isoformat()}:{strategy_id}:{ticker}:{side}"
