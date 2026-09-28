"""Cross-block value types.

Kept minimal and boundary-independent so that ``ingest``, ``backtest``,
``registry``, ``execution``, and ``production`` all agree on what an Order,
a Fill, and a Portfolio are.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

OrderSide = Literal["buy", "sell"]
OrderType = Literal["market", "limit", "stop", "stop_limit"]
OrderStatus = Literal["pending", "filled", "partially_filled", "rejected", "cancelled"]
#: Whether an order opens (or grows) a position or closes (shrinks) one.
#: ``sell`` + ``open`` is a short sale, ``buy`` + ``close`` a cover
#: (``docs/design/shorting.md``).
PositionEffect = Literal["open", "close"]
_POSITION_EFFECTS = ("open", "close")
#: How long a working order lives at the broker (roadmap 19.1): ``day``
#: (the session), ``gtc`` (until cancelled), ``opg`` (the opening auction
#: only) or ``ioc`` (fill now or cancel). ``None`` leaves it to the broker's
#: default for the order type.
TimeInForce = Literal["day", "gtc", "opg", "ioc"]
_TIMES_IN_FORCE = ("day", "gtc", "opg", "ioc")

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
    #: The portfolio the order trades for (``None``: the caller's only book).
    portfolio_id: str | None = None
    # ---- decision context (BL-32, TCA) ----
    #: The price the strategy decided at (the latest close it saw).
    decision_price: float | None = None
    #: When it decided.
    decided_at: datetime | None = None
    #: Why: trigger, signal score and rank, constructor, target weight.
    #: JSON-safe values only. Not part of equality or hashing.
    decision_context: Mapping[str, Any] | None = field(default=None, compare=False)
    #: The cost model's estimate at ``decision_price``, in bps of notional.
    expected_cost_bps: float | None = None
    #: ``open`` or ``close``; ``None`` (legacy) means infer it from the
    #: position when filling. See ``execution.orders.classify``.
    position_effect: PositionEffect | None = None
    # ---- live broker fields (roadmap 19.1) ----
    #: The trigger price of a ``stop`` or ``stop_limit`` order at a live
    #: broker. The backtest's fill model still reads a stop's trigger from
    #: ``limit_price`` when this is ``None``.
    stop_price: float | None = None
    #: ``None``: the broker's default for the order type.
    time_in_force: TimeInForce | None = None
    #: Allow a fill outside regular trading hours. Live adapters refuse
    #: ``True`` in Phase 19.
    outside_rth: bool = False
    #: One-cancels-other group at a live broker (roadmap 19.10): a protective
    #: stop and the exits of the same position share one, so a fill of one
    #: shrinks the others and the position is never sold twice.
    oca_group: str | None = None
    #: How the order is worked (roadmap 23.16): ``None`` for a plain order,
    #: else ``{"name": ..., "params": {...}}`` naming a registered execution
    #: algo (``stonks.execution.algos``). JSON-safe values only.
    algo: Mapping[str, Any] | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if not (_finite(self.quantity) and self.quantity > 0):
            raise ValueError(f"Order.quantity must be positive and finite, got {self.quantity}")
        for name in ("limit_price", "stop_price"):
            value = getattr(self, name)
            if value is not None and not (_finite(value) and value > 0):
                raise ValueError(f"Order.{name} must be a positive finite number, got {value!r}")
        if self.time_in_force is not None and self.time_in_force not in _TIMES_IN_FORCE:
            raise ValueError(
                f"Order.time_in_force must be one of {_TIMES_IN_FORCE}, got {self.time_in_force!r}"
            )
        if self.position_effect is not None and self.position_effect not in _POSITION_EFFECTS:
            raise ValueError(
                f"Order.position_effect must be open or close, got {self.position_effect!r}"
            )
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
    #: The broker's id of the execution this fill books (roadmap 19.1).
    #: Unique per portfolio, so booking an execution twice is a no-op.
    broker_exec_id: str | None = None
    #: The currency ``fee`` is in when it differs from the book's base
    #: currency (a commission report in another currency).
    fee_currency: str | None = None

    @property
    def signed_quantity(self) -> float:
        return self.quantity if self.side == "buy" else -self.quantity


@dataclass
class Portfolio:
    cash: float
    positions: dict[str, float] = field(default_factory=dict[str, float])

    def apply_fill(self, fill: Fill) -> None:
        current = self.positions.get(fill.ticker, 0.0)
        new = current + fill.signed_quantity
        if abs(new) < 1e-12:
            self.positions.pop(fill.ticker, None)
        else:
            self.positions[fill.ticker] = new
        cash_delta = -fill.signed_quantity * fill.price - fill.fee
        self.cash += cash_delta

    def long_value(self, prices: Mapping[str, float]) -> float:
        """Market value of the long positions (unpriced ones count as 0)."""
        return sum(q * _mark(prices, t) for t, q in self.positions.items() if q > 0)

    def short_value(self, prices: Mapping[str, float]) -> float:
        """Market value of the short positions, as a positive magnitude."""
        return sum(-q * _mark(prices, t) for t, q in self.positions.items() if q < 0)

    def gross(self, prices: Mapping[str, float]) -> float:
        """Long value plus short value."""
        return self.long_value(prices) + self.short_value(prices)

    def net(self, prices: Mapping[str, float]) -> float:
        """Long value minus short value."""
        return self.long_value(prices) - self.short_value(prices)

    def unmarked(self, prices: Mapping[str, float]) -> list[str]:
        """Held tickers with no usable price in ``prices`` (absent or not a
        finite number), sorted. Callers decide what to do: carry the last
        mark forward, skip a risk rule, or refuse to trade."""
        return sorted(t for t in self.positions if not _finite(prices.get(t)))

    def total_value(self, prices: Mapping[str, float]) -> float:
        """Cash plus every position marked at ``prices``. A held ticker with
        no price counts as 0: callers that must not read a holding as worth
        nothing check :meth:`unmarked` first (the risk path carries the last
        close, BE-45)."""
        mark = sum(qty * prices.get(t, 0.0) for t, qty in self.positions.items())
        return self.cash + mark


def _mark(prices: Mapping[str, float], ticker: str) -> float:
    price = prices.get(ticker)
    return float(price) if _finite(price) else 0.0  # type: ignore[arg-type]


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True)
class Features:
    values: Mapping[str, float] = field(default_factory=dict[str, float])

    def get(self, name: str, default: Any = None) -> Any:
        return self.values.get(name, default)

    def __len__(self) -> int:
        return len(self.values)

    def __iter__(self):
        return iter(self.values)
