"""Broker adapters behind the ``core.protocols.Broker`` seam.

``make_broker`` is the single place that turns settings into a broker, so
the production tick only has to call it and never imports a vendor SDK.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.types import Portfolio
from stonks.execution.brokers.alpaca import AlpacaBroker
from stonks.execution.brokers.base import (
    BrokerAccount,
    BrokerError,
    BrokerKind,
    BrokerOrderState,
    LiveTradingRefusedError,
    MarketClock,
    OrderRejectedError,
    OrderStateSource,
    UnsupportedTickerError,
)
from stonks.execution.brokers.simulated import SimulatedCosts

if TYPE_CHECKING:  # pragma: no cover
    from stonks.config import Settings
    from stonks.execution.brokers.ibkr.broker import IbkrBroker

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
    "SimulatedCosts",
    "UnsupportedTickerError",
    "make_broker",
]


def make_broker(
    settings: Settings,
    portfolio: Portfolio,
    *,
    kind: BrokerKind | None = None,
) -> SimulatedBroker | AlpacaBroker | IbkrBroker:
    """Build the broker named by ``kind`` (default ``settings.brokers.kind``).

    ``simulated`` trades against ``portfolio`` in memory with the costs
    ``SimulatedCosts.from_settings`` resolves (``[backtest.costs]`` when
    configured, else the legacy ``[production]`` slippage/fee); the caller
    must still ``set_prices``. ``alpaca``
    ignores ``portfolio``: the broker account is the source of truth, read
    it with ``fetch_portfolio()``. ``ibkr`` builds the Interactive Brokers
    adapter for the default portfolio's gateway (``[brokers.ibkr]``); it
    connects on first use and checks the account then.
    """
    kind = kind or settings.brokers.kind
    if kind == "simulated":
        return SimulatedCosts.from_settings(settings).build_broker(portfolio)
    if kind == "ibkr":
        from stonks.accounts.models import DEFAULT_PORTFOLIO_ID
        from stonks.execution.brokers.ibkr.factory import connect_ibkr, pick_gateway
        from stonks.store.state import SqliteState

        pick_gateway(settings.brokers.ibkr, portfolio_id=DEFAULT_PORTFOLIO_ID)
        state = SqliteState(settings.state.path)
        try:
            broker = connect_ibkr(
                settings.brokers.ibkr, portfolio_id=DEFAULT_PORTFOLIO_ID, role="tick", state=state
            )
        except Exception:
            state.close()
            raise
        broker.on_close(state.close)
        return broker
    if kind == "alpaca":
        cfg = settings.brokers.alpaca
        return AlpacaBroker.connect(
            cfg.api_key.get_secret_value() if cfg.api_key else None,
            cfg.secret_key.get_secret_value() if cfg.secret_key else None,
            paper=cfg.paper,
            allow_live=cfg.allow_live,
            allow_short=cfg.allow_short,
            max_retries=cfg.max_retries,
            retry_backoff_seconds=cfg.retry_backoff_seconds,
        )
    raise ValueError(f"unknown broker kind {kind!r}")
