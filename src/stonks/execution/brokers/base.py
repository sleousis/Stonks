"""Broker-side types shared by every broker adapter.

These are *our* types: adapters translate vendor objects into them so no
vendor SDK type ever crosses the ``execution.brokers`` seam.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from stonks.core.types import Fill, Order, OrderSide, OrderStatus

#: Which broker the production tick trades through (``[brokers].kind``).
#: ``ibkr`` is the Interactive Brokers adapter (roadmap 19.2).
BrokerKind = Literal["simulated", "alpaca", "ibkr"]
#: Whose money the default book trades: the in-memory ``simulated`` broker,
#: a broker's ``paper`` account, or a ``live`` (real money) account.
BrokerMode = Literal["simulated", "paper", "live"]
#: The kind of a real-money account (roadmap 19.7).
AccountType = Literal["cash", "margin"]
#: The fine state of an order at a live broker (roadmap 19.1). The ledger's
#: ``OrderStatus`` follows it; the machine is ``execution.order_state``.
OrderState = Literal[
    "pending",
    "submitted",
    "accepted",
    "partially_filled",
    "filled",
    "pending_cancel",
    "cancelled",
    "expired",
    "rejected",
    "unknown",
]
ORDER_STATES: tuple[OrderState, ...] = (
    "pending",
    "submitted",
    "accepted",
    "partially_filled",
    "filled",
    "pending_cancel",
    "cancelled",
    "expired",
    "rejected",
    "unknown",
)


class BrokerError(RuntimeError):
    """A broker call failed after retries (or failed non-retryably)."""


class UnsupportedTickerError(ValueError):
    """The ticker cannot be traded through this broker (e.g. non-US listing)."""


class LiveTradingRefusedError(RuntimeError):
    """A live (real-money) endpoint was requested without ``allow_live=True``."""


class OrderRejectedError(BrokerError):
    """A pre-trade check refused the order before it was sent (blocked
    account, untradable asset, quantity below the broker minimum, ...)."""


class BrokerUnavailableError(BrokerError):
    """The broker could not be reached (gateway down, link to the broker
    lost, competing session). A short outage: the book skips and alerts,
    and only a repeated outage pauses auto mode (roadmap 19.4)."""


class OrderOutcomeUnknownError(BrokerUnavailableError):
    """A submit whose outcome is not known: the link dropped or timed out
    after the order may have left. The caller marks the order ``unknown``
    (``execution.order_state.mark_unknown``) and sends nothing for it again
    until reconciliation finds it by client id."""

    def __init__(self, client_id: str, message: str) -> None:
        self.client_id = client_id
        super().__init__(message)


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
    #: The fine state when the broker reports one (``status`` is the
    #: coarse one every broker gives).
    state: OrderState | None = None


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


@runtime_checkable
class OrderCanceller(Protocol):
    """Optional broker capability: cancel a working order by our client_id.

    Returns ``False`` when there is nothing to cancel (unknown or already
    terminal). The kill switch uses it (``execution.cancel``) so orders a
    tick queued at the broker don't fill after trading was stopped."""

    def cancel_order(self, client_id: str) -> bool: ...


# ---- live broker capabilities (roadmap 19.1) -------------------------------------


@dataclass(frozen=True)
class LiveAccountState:
    """A real account's balances as the broker reports them. The broker is
    the source of truth for cash, settled cash, buying power and day trades
    remaining; our own counters only explain a refusal early."""

    #: Net liquidation value.
    equity: float
    #: Total cash in the base currency.
    cash: float
    settled_cash: float
    available_funds: float
    buying_power: float
    #: The account's base currency.
    currency: str
    account_type: AccountType
    excess_liquidity: float | None = None
    #: US margin accounts under the pattern day trader rule.
    day_trades_remaining: int | None = None
    initial_margin: float = 0.0
    maintenance_margin: float = 0.0
    #: Cash per currency (an EU or UK account often holds USD too).
    cash_by_currency: Mapping[str, float] = field(default_factory=dict[str, float])
    #: The broker's account id (never logged or labelled in metrics).
    account_id: str | None = None

    def cash_in(self, currency: str) -> float:
        """Cash held in ``currency``: the per-currency ledger when the broker
        sent one, else ``cash`` for the base currency and 0 otherwise."""
        if self.cash_by_currency:
            return float(self.cash_by_currency.get(currency, 0.0))
        return self.cash if currency == self.currency else 0.0


@dataclass(frozen=True)
class MarginPreview:
    """The broker's what-if answer for one order: nothing is transmitted."""

    client_id: str
    initial_margin_change: float
    maintenance_margin_change: float
    equity_with_loan_after: float
    commission: float | None
    commission_currency: str | None = None
    #: The broker refused the order in the preview (its text).
    warning: str | None = None


@dataclass(frozen=True)
class Quote:
    """A market data snapshot for one ticker. ``delayed`` is true for a
    delayed feed (no subscription): a price band never treats it as live."""

    ticker: str
    last: float | None
    bid: float | None
    ask: float | None
    as_of: datetime
    delayed: bool

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None or self.bid <= 0 or self.ask <= 0:
            return None
        return (self.bid + self.ask) / 2.0

    @property
    def reference(self) -> float | None:
        """The last trade, else the mid, else ``None``."""
        if self.last is not None and self.last > 0:
            return self.last
        return self.mid


@dataclass(frozen=True)
class Execution:
    """One broker execution, keyed by the broker's execution id. The
    commission arrives later at some brokers, so it may be ``None`` first
    and booked onto the fill when it comes."""

    broker_exec_id: str
    #: Our client id (the broker's order reference mapped back).
    client_id: str
    ticker: str
    side: OrderSide
    quantity: float
    price: float
    executed_at: datetime
    commission: float | None = None
    commission_currency: str | None = None

    def to_fill(self) -> Fill:
        return Fill(
            order_client_id=self.client_id,
            ticker=self.ticker,
            quantity=self.quantity,
            price=self.price,
            fee=self.commission or 0.0,
            filled_at=self.executed_at,
            side=self.side,
            broker_exec_id=self.broker_exec_id,
            fee_currency=self.commission_currency,
        )


@runtime_checkable
class GlobalCanceller(Protocol):
    """Optional capability: cancel every working order in the account,
    including ones placed by hand (the kill switch in stop-all mode).
    Returns how many cancels were requested."""

    def cancel_all(self) -> int: ...


@runtime_checkable
class AccountReader(Protocol):
    """Optional capability: the account's live balances."""

    def fetch_account(self) -> LiveAccountState: ...


@runtime_checkable
class MarginPreviewer(Protocol):
    """Optional capability: a what-if preview of an order. A preview that
    fails or times out must never let a buy through."""

    def what_if(self, order: Order) -> MarginPreview: ...


@runtime_checkable
class ExecutionSource(Protocol):
    """Optional capability: executions (with commissions when known) since
    ``since``. Reconciliation books one fill per execution id."""

    def executions(self, since: datetime) -> Sequence[Execution]: ...


@runtime_checkable
class QuoteSource(Protocol):
    """Optional capability: market data snapshots for a few tickers. A
    ticker with no data is left out of the answer."""

    def quotes(self, tickers: Sequence[str]) -> Mapping[str, Quote]: ...


@dataclass(frozen=True)
class BrokerOpenOrder:
    """A working order in the broker account, ours or placed by hand
    (roadmap 19.5). ``client_id`` is ``None`` when the order carries no
    reference of ours: the owner placed it in TWS or the mobile app."""

    broker_order_id: str
    client_id: str | None
    ticker: str
    side: OrderSide
    quantity: float
    filled_quantity: float = 0.0
    state: OrderState | None = None


@runtime_checkable
class OpenOrderSource(Protocol):
    """Optional capability: every working order in the account, including
    hand-placed ones. Reconciliation compares them with the ledger."""

    def open_orders(self) -> Sequence[BrokerOpenOrder]: ...


#: Capability name per protocol, for logs, health and the console.
CAPABILITIES: tuple[tuple[str, type], ...] = (
    ("order_state", OrderStateSource),
    ("cancel", OrderCanceller),
    ("global_cancel", GlobalCanceller),
    ("account", AccountReader),
    ("what_if", MarginPreviewer),
    ("executions", ExecutionSource),
    ("quotes", QuoteSource),
    ("open_orders", OpenOrderSource),
)


def broker_capabilities(broker: object) -> frozenset[str]:
    """The optional capabilities ``broker`` implements, by name."""
    return frozenset(name for name, proto in CAPABILITIES if isinstance(broker, proto))


def close_broker(broker: object | None) -> None:
    """Close a broker built for one request (its gateway session, say).
    A broker without ``close`` has nothing to release. Never raises: the
    request's outcome matters more than the close."""
    close = getattr(broker, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception as exc:
        from stonks.logging import get_logger

        get_logger("stonks.execution.brokers").warning("broker.close_failed", error=str(exc))
