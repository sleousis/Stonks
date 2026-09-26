"""Broker adapters behind the ``core.protocols.Broker`` seam.

``make_broker`` is the single place that turns settings into a broker, so
the production tick only has to call it and never imports a vendor SDK.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.types import Portfolio
from stonks.execution.brokers.alpaca import AlpacaBroker
from stonks.execution.brokers.base import (
    BrokerAccount,
    BrokerError,
    BrokerOrderState,
    LiveTradingRefusedError,
    MarketClock,
    OrderRejectedError,
    OrderStateSource,
    UnsupportedTickerError,
)

if TYPE_CHECKING:  # pragma: no cover
    from stonks.config import Settings

BrokerKind = Literal["simulated", "alpaca"]

__all__ = [
    "AlpacaBroker",
    "BrokerAccount",
    "BrokerError",
    "BrokerKind",
    "BrokerOrderState",
    "LiveTradingRefusedError",
    "MarketClock",
    "OrderRejectedError",
    "OrderStateSource",
    "SimulatedBroker",
    "UnsupportedTickerError",
    "make_broker",
]


def make_broker(
    settings: Settings,
    portfolio: Portfolio,
    *,
    kind: BrokerKind | None = None,
) -> SimulatedBroker | AlpacaBroker:
    """Build the broker named by ``kind`` (default ``settings.brokers.kind``).

    ``simulated`` trades against ``portfolio`` in memory with the production
    slippage/fee settings (the caller must still ``set_prices``). ``alpaca``
    ignores ``portfolio``: the broker account is the source of truth, read
    it with ``fetch_portfolio()``.
    """
    kind = kind or settings.brokers.kind
    if kind == "simulated":
        return SimulatedBroker(
            portfolio=portfolio,
            slippage_bps=settings.production.slippage_bps,
            fee_per_trade=settings.production.fee_per_trade,
        )
    if kind == "alpaca":
        cfg = settings.brokers.alpaca
        return AlpacaBroker.connect(
            cfg.api_key.get_secret_value() if cfg.api_key else None,
            cfg.secret_key.get_secret_value() if cfg.secret_key else None,
            paper=cfg.paper,
            allow_live=cfg.allow_live,
            max_retries=cfg.max_retries,
            retry_backoff_seconds=cfg.retry_backoff_seconds,
        )
    raise ValueError(f"unknown broker kind {kind!r}")
