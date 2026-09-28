"""Order sizes rounded to tradable lots (roadmap 23.1, P21).

Most brokers trade whole shares of a stock. A backtest that buys 2.7
shares measures a book no one can hold, and a small account skips or
rounds most of its orders. The lot rule sizes every order to the lots the
broker takes, in the one order pipeline the backtest, the paper books and
the live books share (:func:`stonks.portfolio.pipeline.build_orders`), so
all three trade the same shares.

A **lot profile** says, per asset class, whether orders are whole lots or
fractional. Profiles live in a registry (:data:`LOT_PROFILES`):

- ``fractional``: no rounding (the default, and today's behaviour);
- ``whole_shares``: whole lots for equities, bonds and commodities,
  fractional crypto. IBKR works this way (its crypto trades in fractions);
- ``whole``: whole lots for every asset class.

A lot is one unit unless ``lot_sizes`` names another for a ticker (a
board lot of 100 on some exchanges). Option contract ids are left alone:
contracts are whole already.

The rule, per order, for a whole-lot asset class:

- a close of the whole position keeps its exact quantity, so a book never
  strands a fraction it already holds;
- every other quantity rounds **down** to whole lots, so an order never
  spends more cash or takes more risk than was decided;
- an order below one lot is skipped.

Which profile a book uses:

- backtests, the lab and paper books: ``[backtest.lots] profile``, one
  setting for all of them, so a paper book and its backtest stay in step
  (P21). It defaults to ``fractional`` so existing results do not move;
- live books: their broker's profile (:func:`broker_lot_profile`), since
  the broker only takes what it takes (IBKR: ``whole_shares``).

Set ``[backtest.lots] profile = "whole_shares"`` to test a strategy the
way an IBKR account will trade it.

:class:`LotStats` gathers what the rounding did over a backtest: orders
rounded and skipped, the weight lost to rounding (drift), and the
**minimum capital**: the book size at which a chosen share
(``min_capital_quantile``, 95% by default) of the opening orders buys at
least one lot. An order for ``q`` shares decided on a book worth ``E``
needs ``E * lot / q`` to buy one lot, whatever the price. The figure is
measured against ``min_capital_profile`` (``whole_shares`` by default)
even when the orders themselves stay fractional.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, field_validator

from stonks.core.options import is_option_id
from stonks.core.types import Order

#: Tolerance for float noise: 2.9999999999 shares is three shares.
_EPS = 1e-9


@dataclass(frozen=True)
class LotProfile:
    name: str
    #: Asset classes sized to whole lots; every other class is fractional.
    whole: frozenset[str]
    description: str = ""


#: Lot profiles by name. A new profile is one :func:`register_profile` call.
LOT_PROFILES: dict[str, LotProfile] = {}


def register_profile(profile: LotProfile) -> LotProfile:
    LOT_PROFILES[profile.name] = profile
    return profile


register_profile(LotProfile("fractional", frozenset(), "No rounding: any fraction trades."))
register_profile(
    LotProfile(
        "whole_shares",
        frozenset({"equity", "bond", "commodity"}),
        "Whole lots for stocks, bonds and commodities, fractional crypto (IBKR).",
    )
)
register_profile(
    LotProfile(
        "whole",
        frozenset({"equity", "bond", "commodity", "crypto"}),
        "Whole lots for every asset class.",
    )
)

#: The lot profile of each live broker. A broker not named here gets
#: ``whole_shares``: every broker takes whole shares.
BROKER_LOT_PROFILES: dict[str, str] = {
    "ibkr": "whole_shares",
    # Alpaca takes fractional shares (market and day orders) and crypto.
    "alpaca": "fractional",
}


def broker_lot_profile(broker_kind: str) -> str | None:
    """The profile a live book at ``broker_kind`` must use; ``None`` for the
    simulated broker (it follows ``[backtest.lots]``)."""
    if broker_kind == "simulated":
        return None
    return BROKER_LOT_PROFILES.get(broker_kind, "whole_shares")


def _known_profile(name: str) -> str:
    if name not in LOT_PROFILES:
        raise ValueError(f"unknown lot profile {name!r}; choose from {sorted(LOT_PROFILES)}")
    return name


class LotSettings(BaseModel):
    """``[backtest.lots]``: how backtests, the lab and paper books round
    order sizes to lots (see the module doc)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The profile of backtests, the lab and paper books.
    profile: str = "fractional"
    #: Lot size per ticker where it is not one unit.
    lot_sizes: dict[str, float] = Field(default_factory=dict)
    #: The profile the minimum capital figure is measured against.
    min_capital_profile: str = "whole_shares"
    #: Share of opening orders that must buy at least one lot.
    min_capital_quantile: float = Field(default=0.95, gt=0.0, le=1.0)

    @field_validator("profile", "min_capital_profile")
    @classmethod
    def _known(cls, name: str) -> str:
        return _known_profile(name)

    @field_validator("lot_sizes")
    @classmethod
    def _positive(cls, sizes: dict[str, float]) -> dict[str, float]:
        bad = {t: s for t, s in sizes.items() if not (math.isfinite(s) and s > 0)}
        if bad:
            raise ValueError(f"lot sizes must be positive: {bad}")
        return sizes

    def rule(self, profile: str | None = None) -> LotRule:
        """The rule of ``profile`` (default: this setting's ``profile``)."""
        name = _known_profile(profile or self.profile)
        return LotRule(LOT_PROFILES[name], dict(self.lot_sizes))


@dataclass(frozen=True)
class LotChange:
    """One order the rule rounded (``sized > 0``) or skipped (``sized == 0``)."""

    client_id: str
    ticker: str
    side: str
    requested: float
    sized: float
    price: float

    @property
    def skipped(self) -> bool:
        return self.sized <= 0

    @property
    def notional(self) -> float:
        """Money value of what the rounding removed."""
        return abs(self.requested - self.sized) * self.price


@dataclass(frozen=True)
class LotResult:
    orders: list[Order]
    changes: list[LotChange] = field(default_factory=list)
    #: The orders as they came in, before rounding.
    requested: list[Order] = field(default_factory=list)

    @property
    def skipped(self) -> list[LotChange]:
        return [c for c in self.changes if c.skipped]

    def drift(self, equity: float) -> float:
        """Weight of the book lost to rounding and skips (a fraction)."""
        if not equity > 0:
            return 0.0
        return sum(c.notional for c in self.changes) / equity

    def summary(self) -> dict[str, list[str]]:
        """Tickers rounded and skipped, for a tick summary."""
        out: dict[str, list[str]] = {}
        skipped = sorted({c.ticker for c in self.changes if c.skipped})
        rounded = sorted({c.ticker for c in self.changes if not c.skipped})
        if skipped:
            out["lot_skipped"] = skipped
        if rounded:
            out["lot_rounded"] = rounded
        return out


@dataclass(frozen=True)
class LotRule:
    profile: LotProfile
    lot_sizes: Mapping[str, float] = field(default_factory=dict)

    @property
    def rounds(self) -> bool:
        return bool(self.profile.whole)

    def lot(self, ticker: str, asset_class: str | None) -> float | None:
        """The lot of ``ticker``; ``None`` when it trades in fractions."""
        if is_option_id(ticker) or (asset_class or "equity") not in self.profile.whole:
            return None
        return float(self.lot_sizes.get(ticker, 1.0))

    def size(
        self,
        orders: Sequence[Order],
        positions: Mapping[str, float],
        prices: Mapping[str, float],
        asset_classes: Mapping[str, str],
    ) -> LotResult:
        """``orders`` sized to lots against the book's ``positions``."""
        requested = list(orders)
        if not self.rounds:
            return LotResult(orders=requested, requested=requested)
        out: list[Order] = []
        changes: list[LotChange] = []
        for order in requested:
            lot = self.lot(order.ticker, asset_classes.get(order.ticker))
            if lot is None or _closes_all(order, positions):
                out.append(order)
                continue
            sized = math.floor(order.quantity / lot + _EPS) * lot
            if sized > 0 and abs(sized - order.quantity) <= _EPS * max(1.0, order.quantity):
                # float noise, not a fraction: snap it to the lot silently
                out.append(order if sized == order.quantity else replace(order, quantity=sized))
                continue
            price = prices.get(order.ticker)
            changes.append(
                LotChange(
                    client_id=order.client_id,
                    ticker=order.ticker,
                    side=order.side,
                    requested=order.quantity,
                    sized=max(sized, 0.0),
                    price=float(price) if price is not None and math.isfinite(price) else 0.0,
                )
            )
            if sized > 0:
                out.append(replace(order, quantity=sized))
        return LotResult(orders=out, changes=changes, requested=requested)


def _closes_all(order: Order, positions: Mapping[str, float]) -> bool:
    held = positions.get(order.ticker, 0.0)
    if held == 0:
        return False
    closing = (order.side == "sell") == (held > 0)
    return closing and abs(order.quantity - abs(held)) <= _EPS * max(1.0, abs(held))


def _opens(order: Order, positions: Mapping[str, float]) -> bool:
    if order.position_effect is not None:
        return order.position_effect == "open"
    held = positions.get(order.ticker, 0.0)
    return held >= 0 if order.side == "buy" else held <= 0


@dataclass(frozen=True)
class LotReport:
    """What lot rounding did over one backtest (see the module doc)."""

    profile: str
    orders: int = 0
    rounded: int = 0
    skipped: int = 0
    skipped_notional: float = 0.0
    #: Mean and largest weight lost to rounding per decision with orders.
    mean_drift: float = 0.0
    max_drift: float = 0.0
    #: Book size at which the quantile of opening orders buys one lot.
    min_capital: float | None = None
    min_capital_profile: str = "whole_shares"

    @property
    def skipped_share(self) -> float:
        return self.skipped / self.orders if self.orders else 0.0

    def metrics(self) -> dict[str, float]:
        """Flat numbers for a survival report or an API view."""
        out = {
            "lot_orders": float(self.orders),
            "lot_rounded_orders": float(self.rounded),
            "lot_skipped_orders": float(self.skipped),
            "lot_skipped_share": self.skipped_share,
            "lot_skipped_notional": self.skipped_notional,
            "lot_mean_drift": self.mean_drift,
            "lot_max_drift": self.max_drift,
        }
        if self.min_capital is not None:
            out["min_capital"] = self.min_capital
        return out

    def to_dict(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "orders": self.orders,
            "rounded": self.rounded,
            "skipped": self.skipped,
            "skipped_share": self.skipped_share,
            "skipped_notional": self.skipped_notional,
            "mean_drift": self.mean_drift,
            "max_drift": self.max_drift,
            "min_capital": self.min_capital,
            "min_capital_profile": self.min_capital_profile,
        }


class LotStats:
    """Gathers lot results over a backtest's decisions."""

    def __init__(self, settings: LotSettings) -> None:
        self._settings = settings
        self._reference = settings.rule(settings.min_capital_profile)
        self._orders = self._rounded = self._skipped = 0
        self._skipped_notional = 0.0
        self._drifts: list[float] = []
        self._capital: list[float] = []

    def record(
        self,
        requested: Sequence[Order],
        result: LotResult,
        positions: Mapping[str, float],
        asset_classes: Mapping[str, str],
        equity: float,
    ) -> None:
        if not requested:
            return
        self._orders += len(requested)
        self._skipped += len(result.skipped)
        self._rounded += len(result.changes) - len(result.skipped)
        self._skipped_notional += sum(c.requested * c.price for c in result.skipped)
        self._drifts.append(result.drift(equity))
        if not equity > 0:
            return
        for order in requested:
            lot = self._reference.lot(order.ticker, asset_classes.get(order.ticker))
            if lot is None or not _opens(order, positions):
                continue
            self._capital.append(equity * lot / order.quantity)

    def report(self) -> LotReport:
        capital = None
        if self._capital:
            capital = float(np.quantile(self._capital, self._settings.min_capital_quantile))
        return LotReport(
            profile=self._settings.profile,
            orders=self._orders,
            rounded=self._rounded,
            skipped=self._skipped,
            skipped_notional=self._skipped_notional,
            mean_drift=float(np.mean(self._drifts)) if self._drifts else 0.0,
            max_drift=max(self._drifts, default=0.0),
            min_capital=capital,
            min_capital_profile=self._settings.min_capital_profile,
        )
