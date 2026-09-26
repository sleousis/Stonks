"""Transaction cost models for the ``SimulatedBroker``.

``CostModel`` is the seam: given a ``Trade`` (what is being traded, at what
reference price, in which asset class, against how much bar volume) it
returns a ``TradeCost`` — the fill price after spread / slippage / impact
and the fee charged for the fill. The broker applies it; strategies never
see it.

Implementations
---------------
- ``FixedCostModel`` — the legacy behaviour: ``slippage_bps`` in the adverse
  direction plus a flat ``fee_per_trade``. ``SimulatedBroker``'s
  ``slippage_bps`` / ``fee_per_trade`` arguments build one of these.
- ``AssetClassCostModel`` — built from ``CostModelSettings``:

  * per asset class (falling back to ``default``): a fee of
    ``fee_flat + fee_bps * fill notional`` and a ``half_spread_bps`` paid
    in the adverse direction;
  * volume-aware market impact, the square-root law
    ``impact_bps * sqrt(quantity / bar_volume)`` — ``impact_bps`` is the
    impact of trading the whole bar's volume — capped at
    ``max_impact_bps``. A zero-volume bar pays the cap (nothing traded, so
    any fill is maximally illiquid). Unknown volume (``None``, e.g. a
    production tick that has no bar volume) adds no impact.

Contract: a model's fill price and fee must be non-decreasing in
``quantity`` on the adverse side. The broker relies on this to scale
unaffordable buys down (a smaller order never costs more per unit).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks.core.types import AssetClass, OrderSide

_BPS = 10_000.0


@dataclass(frozen=True)
class Trade:
    ticker: str
    side: OrderSide
    quantity: float
    #: Reference price before costs (the bar open the order fills at).
    price: float
    asset_class: AssetClass = "equity"
    #: Units traded in the bar the order fills in; ``None`` when unknown.
    bar_volume: float | None = None


@dataclass(frozen=True)
class TradeCost:
    fill_price: float
    fee: float


@runtime_checkable
class CostModel(Protocol):
    def cost(self, trade: Trade) -> TradeCost: ...


def _adverse(price: float, side: OrderSide, bps: float) -> float:
    shift = price * bps / _BPS
    return price + shift if side == "buy" else price - shift


class FixedCostModel:
    """Flat per-trade fee plus constant adverse slippage in bps."""

    def __init__(self, slippage_bps: float = 0.0, fee_per_trade: float = 0.0) -> None:
        self._slippage_bps = slippage_bps
        self._fee = fee_per_trade

    def cost(self, trade: Trade) -> TradeCost:
        return TradeCost(
            fill_price=_adverse(trade.price, trade.side, self._slippage_bps),
            fee=self._fee,
        )


class AssetClassCosts(BaseModel):
    """Fee and spread for one asset class."""

    model_config = ConfigDict(frozen=True)

    fee_flat: float = Field(0.0, ge=0.0)
    fee_bps: float = Field(0.0, ge=0.0)
    half_spread_bps: float = Field(0.0, ge=0.0)


class CostModelSettings(BaseModel):
    """Settings for ``AssetClassCostModel``. Zero costs by default;
    ``CostModelSettings.realistic()`` is a sensible starting point."""

    model_config = ConfigDict(frozen=True)

    default: AssetClassCosts = AssetClassCosts()
    asset_classes: dict[AssetClass, AssetClassCosts] = Field(default_factory=dict)
    #: Square-root-law impact, in bps, of an order the size of the whole bar.
    impact_bps: float = Field(0.0, ge=0.0)
    #: Cap on the impact term, in bps.
    max_impact_bps: float = Field(500.0, ge=0.0)

    @model_validator(mode="after")
    def _sell_prices_stay_positive(self) -> CostModelSettings:
        for costs in (self.default, *self.asset_classes.values()):
            if costs.half_spread_bps + self.max_impact_bps >= _BPS:
                raise ValueError(
                    "half_spread_bps + max_impact_bps must be below 10000 "
                    "or sell fills would be at a non-positive price"
                )
        return self

    def for_asset_class(self, asset_class: AssetClass) -> AssetClassCosts:
        return self.asset_classes.get(asset_class, self.default)

    def build(self) -> AssetClassCostModel:
        return AssetClassCostModel(self)

    @classmethod
    def realistic(cls) -> CostModelSettings:
        """Retail-broker-ish defaults: commission-free equities with a tight
        spread, taker fees on crypto, a per-contract-style flat fee on
        commodities, wider spreads on bonds, and 100 bps of impact at full
        bar participation (roughly half a daily volatility)."""
        return cls(
            default=AssetClassCosts(half_spread_bps=2.0, fee_bps=0.5),
            asset_classes={
                "equity": AssetClassCosts(half_spread_bps=2.0, fee_bps=0.5),
                "crypto": AssetClassCosts(half_spread_bps=5.0, fee_bps=10.0),
                "commodity": AssetClassCosts(half_spread_bps=3.0, fee_flat=1.0),
                "bond": AssetClassCosts(half_spread_bps=5.0, fee_bps=1.0),
            },
            impact_bps=100.0,
            max_impact_bps=500.0,
        )


class AssetClassCostModel:
    """Per-asset-class fee and half-spread plus square-root market impact."""

    def __init__(self, settings: CostModelSettings) -> None:
        self._settings = settings

    def cost(self, trade: Trade) -> TradeCost:
        costs = self._settings.for_asset_class(trade.asset_class)
        adverse_bps = costs.half_spread_bps + self._impact_bps(trade)
        fill_price = _adverse(trade.price, trade.side, adverse_bps)
        fee = costs.fee_flat + costs.fee_bps / _BPS * fill_price * trade.quantity
        return TradeCost(fill_price=fill_price, fee=fee)

    def _impact_bps(self, trade: Trade) -> float:
        s = self._settings
        volume = trade.bar_volume
        if s.impact_bps == 0.0 or volume is None or math.isnan(volume):
            return 0.0
        if volume <= 0:
            return s.max_impact_bps
        return min(s.impact_bps * math.sqrt(trade.quantity / volume), s.max_impact_bps)
