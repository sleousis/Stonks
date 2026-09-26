"""Order-id helpers shared by backtest and production.

``make_client_id`` assembles a deterministic, idempotency-friendly identifier
from the (as_of date, strategy, ticker, side) tuple. It deliberately does not
include the per-run ``tick_id``: a crashed tick re-run for the same ``as_of``
must reproduce the same client_ids so already-submitted orders can be
recognized and skipped. Any broker honoring the Broker Protocol must
short-circuit duplicate submissions of the same client_id.

A portfolio other than the default one adds its id
(``<as_of>:<portfolio_id>:<strategy>:<ticker>:<side>``), so two portfolios
trading the same strategy and ticker on one day never share a client id.
The default portfolio keeps the pre-accounts format, so a same-day re-run
across the accounts upgrade still recognises its orders.
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
    portfolio_id: str | None = None,
) -> str:
    """Return ``"<as_of ISO date>:<strategy_id>:<ticker>:<side>"``, with
    ``<portfolio_id>:`` after the date for a non-default portfolio
    (``None`` or the default portfolio: the plain format).

    Stable across re-runs of the same trading day, distinct across days,
    portfolios, strategies, tickers and sides.
    """
    # Imported here: the accounts package imports config, which reaches
    # modules that import this one.
    from stonks.accounts.models import DEFAULT_PORTFOLIO_ID

    if portfolio_id is not None and portfolio_id != DEFAULT_PORTFOLIO_ID:
        return f"{as_of.isoformat()}:{portfolio_id}:{strategy_id}:{ticker}:{side}"
    return f"{as_of.isoformat()}:{strategy_id}:{ticker}:{side}"
