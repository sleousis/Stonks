"""The risk-rule seam (BL-11).

A ``RiskRule`` takes the orders a strategy proposed and returns the ones it
allows plus one ``RiskAdjustment`` per order it clipped or dropped. Rules
only ever reduce exposure: buys may shrink or disappear, sells are never
blocked (only clipped so they cannot open a short).

Rules are found automatically: every module in this package is imported and
each class decorated with ``@register_rule`` joins the registry. A new rule
is one new file; ``apply_risk`` runs every registered rule in ``order``.

Two kinds:

- ``RiskRule.apply(orders, ctx)`` sees the whole order list (e.g. a
  drawdown breaker scaling every buy).
- ``OrderRule`` checks one order at a time against a running ``RiskBook``
  (cash and positions after the orders already allowed). Consecutive
  order rules run as one pass: sells first, then buys, each order through
  every rule before the next order is looked at, so a dropped buy never
  uses up cash or a position slot. Today's caps (``caps.py``) are order
  rules.

Order of application (``order``; lower first):

1. ``max_holding`` (1): forced sells of positions held too long;
2. ``drawdown_scaling`` (2): every opening buy times the drawdown size;
3. ``portfolio_vol`` (3): opening buys scaled to the volatility caps,
   measured on the buys drawdown scaling left;
4. one order-rule pass: ``sell_within_position`` (10), ``require_price``
   (20), ``max_open_positions`` (30), ``max_weight_per_ticker`` (40),
   ``max_weight_per_asset_class`` (50), ``risk_per_position`` (52),
   ``sector_cap`` (54), ``liquidity`` (56), ``cash_buffer`` (60) and
   ``min_order_notional`` (70), so cash and the minimum notional see the
   final size.

Every step only shrinks buys, so the result is at most what any single
rule allows. A rule may declare ``enabled(policy)``; disabled rules are
left out. The W3.1 rules (BL-27) are off unless ``policy.rules`` (see
``settings.py``) switches them on.
"""

from __future__ import annotations

import importlib
import math
import pkgutil
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from types import ModuleType
from typing import TYPE_CHECKING, Any, ClassVar, overload

from stonks.backtest.costs import CostModel, Trade
from stonks.core.types import Order, Portfolio
from stonks.logging import get_logger

if TYPE_CHECKING:
    import pandas as pd

    from stonks.config import RiskPolicy

__all__ = [
    "EPS",
    "OrderRule",
    "Recorder",
    "RiskAdjustment",
    "RiskBook",
    "RiskContext",
    "RiskRule",
    "clip",
    "discover_rules",
    "register_rule",
    "registered_rules",
    "run_order_rules",
]

_log = get_logger("stonks.production.rules")

#: Quantities below this are treated as zero (float noise from clipping).
EPS = 1e-9

_SCALE_ITERATIONS = 60


@dataclass(frozen=True)
class RiskAdjustment:
    ticker: str
    side: str
    #: The tag of the check that fired (e.g. ``max_weight_per_ticker``).
    rule: str
    original_quantity: float
    adjusted_quantity: float
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "side": self.side,
            "rule": self.rule,
            "original_quantity": self.original_quantity,
            "adjusted_quantity": self.adjusted_quantity,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RiskContext:
    """Everything a rule may look at. Only the first four fields are always
    filled; the rest come from ``build_risk_context`` (history-aware rules
    declare ``needs_history`` and are skipped without it)."""

    portfolio: Portfolio
    prices: Mapping[str, float]
    asset_classes: Mapping[str, str]
    policy: RiskPolicy
    #: Last adjusted OHLCV bars per ticker (oldest first), indexed by date.
    history: Mapping[str, pd.DataFrame] = field(default_factory=dict)
    sectors: Mapping[str, str] = field(default_factory=dict)
    #: ``(day, total portfolio value)``, oldest first.
    equity_curve: Sequence[tuple[date, float]] = ()
    #: When each held position was opened.
    entry_dates: Mapping[str, date] = field(default_factory=dict)
    #: The fill-cost model the cash estimate uses; ``None`` means the legacy
    #: flat ``slippage_bps`` / ``fee_per_trade``.
    cost_model: CostModel | None = None
    #: Bar volume per ticker (the cost model's impact input).
    volumes: Mapping[str, float] = field(default_factory=dict)
    slippage_bps: float = 0.0
    fee_per_trade: float = 0.0
    #: The decision day. Rules read no bar or equity point after it.
    as_of: date | None = None
    #: The book's portfolio (``None``: the default one); orders a rule
    #: creates carry it in their client id.
    portfolio_id: str | None = None

    def __post_init__(self) -> None:
        if self.cost_model is not None and (self.slippage_bps or self.fee_per_trade):
            raise ValueError(
                "use a cost model or legacy slippage_bps/fee_per_trade, not both "
                "(costs would be counted twice)"
            )

    def buy_outlay(self, ticker: str, quantity: float) -> float:
        """Cash a buy of ``quantity`` is expected to take (notional + fee);
        0 when unpriced (such buys never reach the broker)."""
        price = self.prices.get(ticker)
        if not price or price <= 0:
            return 0.0
        if self.cost_model is None:
            return quantity * (price * (1 + self.slippage_bps / 10_000.0)) + self.fee_per_trade
        cost = self.cost_model.cost(self._trade(ticker, "buy", quantity, price))
        return quantity * cost.fill_price + cost.fee

    def sell_proceeds(self, ticker: str, quantity: float) -> float:
        """Cash a sell of ``quantity`` is expected to bring (0 if unpriced)."""
        price = self.prices.get(ticker)
        if not price or price <= 0:
            return 0.0
        if self.cost_model is None:
            return quantity * price * (1 - self.slippage_bps / 10_000.0) - self.fee_per_trade
        cost = self.cost_model.cost(self._trade(ticker, "sell", quantity, price))
        return quantity * cost.fill_price - cost.fee

    def affordable_quantity(self, ticker: str, quantity: float, budget: float) -> float:
        """The most of a buy of ``quantity`` whose outlay fits in ``budget``:
        ``quantity`` or more when it fits, less (possibly <= 0) otherwise."""
        price = self.prices.get(ticker)
        if not price or price <= 0:
            return quantity
        if self.cost_model is None:
            fill_price = price * (1 + self.slippage_bps / 10_000.0)
            return (budget - self.fee_per_trade) / fill_price
        if self.buy_outlay(ticker, quantity) <= budget:
            return quantity
        if self.buy_outlay(ticker, EPS) > budget:
            return 0.0
        # Fill price and fee are non-decreasing in quantity (CostModel
        # contract), so the affordable set is [0, q*]: bisect for q*.
        lo, hi = 0.0, quantity
        for _ in range(_SCALE_ITERATIONS):
            mid = (lo + hi) / 2
            if self.buy_outlay(ticker, mid) <= budget:
                lo = mid
            else:
                hi = mid
        return lo

    def _trade(self, ticker: str, side: str, quantity: float, price: float) -> Trade:
        volume = self.volumes.get(ticker)
        return Trade(
            ticker=ticker,
            side=side,  # type: ignore[arg-type]
            quantity=quantity,
            price=price,
            asset_class=self.asset_classes.get(ticker, "equity"),  # type: ignore[arg-type]
            bar_volume=None if volume is None or math.isnan(volume) else volume,
        )


@dataclass
class RiskBook:
    """Cash and positions as the allowed orders would leave them. Weights
    are measured against ``equity``: the value before any order."""

    equity: float
    cash: float
    positions: dict[str, float]

    @classmethod
    def open(cls, ctx: RiskContext) -> RiskBook:
        return cls(
            equity=ctx.portfolio.total_value(dict(ctx.prices)),
            cash=ctx.portfolio.cash,
            positions=dict(ctx.portfolio.positions),
        )

    def commit(self, order: Order, qty: float, ctx: RiskContext) -> None:
        if order.side == "sell":
            remaining = self.positions.get(order.ticker, 0.0) - qty
            if remaining <= EPS:
                self.positions.pop(order.ticker, None)
            else:
                self.positions[order.ticker] = remaining
            self.cash += ctx.sell_proceeds(order.ticker, qty)
        else:
            self.positions[order.ticker] = self.positions.get(order.ticker, 0.0) + qty
            self.cash -= ctx.buy_outlay(order.ticker, qty)


#: ``record(order, rule_tag, new_quantity, reason)``.
Recorder = Callable[[Order, str, float, str], None]


class RiskRule(ABC):
    """One risk rule. Subclasses set ``name`` and ``order`` (lower runs
    first) and register with ``@register_rule``. Rules take no constructor
    arguments: limits come from ``ctx.policy``."""

    name: ClassVar[str]
    order: ClassVar[int]
    #: Needs ``RiskContext`` history (bars, equity curve, ...); skipped when
    #: ``apply_risk`` is called without a context.
    needs_history: ClassVar[bool] = False

    def enabled(self, policy: Any) -> bool:
        """Whether ``policy`` switches this rule on. ``apply_risk`` leaves a
        disabled rule out entirely (not run, not reported as skipped)."""
        return True

    @abstractmethod
    def apply(
        self, orders: Sequence[Order], ctx: RiskContext
    ) -> tuple[list[Order], list[RiskAdjustment]]: ...


class OrderRule(RiskRule):
    """A rule that checks one order at a time against the running book."""

    @abstractmethod
    def check(
        self, order: Order, qty: float, book: RiskBook, ctx: RiskContext, record: Recorder
    ) -> float | None:
        """The allowed quantity for ``order`` (currently ``qty``), or
        ``None`` to drop it. Call ``record`` for every change."""

    def apply(
        self, orders: Sequence[Order], ctx: RiskContext
    ) -> tuple[list[Order], list[RiskAdjustment]]:
        return run_order_rules([self], orders, ctx)


def run_order_rules(
    rules: Sequence[OrderRule], orders: Sequence[Order], ctx: RiskContext
) -> tuple[list[Order], list[RiskAdjustment]]:
    """One pass of ``rules`` over ``orders``: sells first, then buys, each
    order through every rule and committed to the book before the next."""
    book = RiskBook.open(ctx)
    kept: list[Order] = []
    adjustments: list[RiskAdjustment] = []

    def record(order: Order, rule: str, new_qty: float, reason: str) -> None:
        adj = RiskAdjustment(
            ticker=order.ticker,
            side=order.side,
            rule=rule,
            original_quantity=order.quantity,
            adjusted_quantity=max(new_qty, 0.0),
            reason=reason,
        )
        adjustments.append(adj)
        _log.info("risk.adjusted", client_id=order.client_id, **adj.as_dict())

    for side in ("sell", "buy"):
        for order in (o for o in orders if o.side == side):
            qty: float | None = order.quantity
            for rule in rules:
                qty = rule.check(order, qty, book, ctx, record)
                if qty is None:
                    break
            if qty is None:
                continue
            kept.append(order if qty == order.quantity else replace(order, quantity=qty))
            book.commit(order, qty, ctx)
    return kept, adjustments


def clip(order: Order, qty: float, max_qty: float, rule: str, record: Recorder) -> float | None:
    """Clip ``qty`` to ``max_qty``; ``None`` when nothing is left."""
    if qty <= max_qty:
        return qty
    new_qty = max(max_qty, 0.0)
    record(order, rule, new_qty, f"quantity {qty} exceeds {rule} room {new_qty}")
    if new_qty <= EPS:
        return None
    return new_qty


# ---- registry -----------------------------------------------------------------

_RULES: dict[str, type[RiskRule]] = {}


@overload
def register_rule(
    cls: type[RiskRule], *, registry: MutableMapping[str, type[RiskRule]] | None = None
) -> type[RiskRule]: ...
@overload
def register_rule(
    cls: None = None, *, registry: MutableMapping[str, type[RiskRule]] | None = None
) -> Callable[[type[RiskRule]], type[RiskRule]]: ...
def register_rule(
    cls: type[RiskRule] | None = None,
    *,
    registry: MutableMapping[str, type[RiskRule]] | None = None,
) -> Any:
    """Class decorator adding a rule to the registry (``@register_rule`` or
    ``@register_rule(registry=...)`` for tests). Names must be unique."""
    target = _RULES if registry is None else registry

    def add(rule_cls: type[RiskRule]) -> type[RiskRule]:
        existing = target.get(rule_cls.name)
        # The same class seen again (its module re-imported) just replaces.
        same = existing is not None and (existing.__module__, existing.__qualname__) == (
            rule_cls.__module__,
            rule_cls.__qualname__,
        )
        if existing is not None and not same:
            raise ValueError(
                f"risk rule name {rule_cls.name!r} is used by both "
                f"{existing.__module__}.{existing.__qualname__} and "
                f"{rule_cls.__module__}.{rule_cls.__qualname__}"
            )
        target[rule_cls.name] = rule_cls
        return rule_cls

    return add if cls is None else add(cls)


def discover_rules(package: ModuleType) -> None:
    """Import every public module of ``package`` so its rules register."""
    for info in pkgutil.iter_modules(package.__path__):
        if not info.name.startswith("_"):
            importlib.import_module(f"{package.__name__}.{info.name}")


def registered_rules() -> list[RiskRule]:
    """One instance of every rule in this package, by ``order`` then name."""
    import stonks.production.rules as package

    discover_rules(package)
    return [cls() for cls in sorted(_RULES.values(), key=lambda c: (c.order, c.name))]
