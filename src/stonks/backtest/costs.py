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
  * volume-aware market impact (``impact_model``), capped at
    ``max_impact_bps``:

    - ``"sqrt"`` (default, legacy) — ``impact_bps * sqrt(quantity /
      bar_volume)``; ``impact_bps`` is the impact of trading the whole
      bar's volume. A zero-volume bar pays the cap (nothing traded, so any
      fill is maximally illiquid). Unknown volume (``None``, e.g. a
      production tick that has no bar volume) adds no impact.
    - ``"sqrt_vol"`` — volatility-scaled square root (Bacidore; Almgren et
      al.): ``impact_gamma * sigma_daily_bps * sqrt(quantity / adv)``.
    - ``"istar"`` — Kissell's I-Star: ``I = a1 (Q/ADV)^a2 sigma^a3`` bps
      with ``sigma`` the **annualised** volatility (decimal), split into a
      temporary part ``b1 I pov^a4`` and a permanent part ``(1 - b1) I``;
      a fill pays both. The defaults are Kissell's published US-equity
      estimates and **need calibration** (against paper fills, BL-32)
      before they are trusted.

    ``sqrt_vol`` and ``istar`` read the trade's **lagged** ``adv`` and
    ``sigma_daily`` (``stonks.backtest.fills.MarketStats``; the fill bar's
    own volume is not used). When either is unknown they fall back to the
    ``sqrt`` formula; a zero ADV pays the cap.
  * per-ticker half-spread (``half_spread_model``): ``"class"`` (default)
    uses the asset class's ``half_spread_bps``; ``"corwin_schultz"`` /
    ``"abdi_ranaldo"`` use the trade's lagged OHLC estimate
    (``stonks.features.spread``) clipped to ``[class half_spread_bps,
    max_half_spread_bps]``, and the class value when there is none.

  The engine computes the lagged statistics only when a model asks for
  them (``market_stats_spec``), so the default settings behave exactly as
  before.
  * per asset class, a broker ``commission`` schedule (``ibkr_fixed``,
    ``ibkr_tiered``) and the US regulatory fees (``us_sell_fees``), added to
    the fee (:mod:`stonks.backtest.commissions`, roadmap 23.2). Both are off
    by default; ``CostModelSettings.ibkr()`` turns them on for equities.

Contract: a model's fill price and fee must be non-decreasing in
``quantity`` on the adverse side. The broker relies on this to scale
unaffordable buys down (a smaller order never costs more per unit).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from stonks.backtest.commissions import (
    COMMISSIONS,
    CommissionSettings,
    commission_fee,
    regulatory_fee,
)
from stonks.backtest.fills import MarketStatsSpec
from stonks.core.types import AssetClass, OrderSide

_BPS = 10_000.0

ImpactModel = Literal["sqrt", "sqrt_vol", "istar"]
HalfSpreadModel = Literal["class", "corwin_schultz", "abdi_ranaldo"]


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
    #: Median volume of the prior bars (lagged); ``None`` when unknown.
    adv: float | None = None
    #: Per-bar std of log returns over the prior bars (lagged, decimal).
    sigma_daily: float | None = None
    #: Per-ticker half-spread estimate in bps (lagged); ``None`` = unknown.
    half_spread_bps: float | None = None
    #: Bars per year of the backtest interval on this asset class's calendar
    #: (``backtest.calendar``), to annualise ``sigma_daily``; ``None`` falls
    #: back to ``IStarSettings.periods_per_year``.
    periods_per_year: float | None = None


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
    #: Broker commission schedule (``stonks.backtest.commissions.COMMISSIONS``).
    commission: str = "none"
    #: Pay the US regulatory fees (SEC and FINRA TAF on sales, CAT on both).
    us_sell_fees: bool = False

    @field_validator("commission")
    @classmethod
    def _known_commission(cls, name: str) -> str:
        if name not in COMMISSIONS:
            raise ValueError(f"unknown commission {name!r}; choose from {sorted(COMMISSIONS)}")
        return name


class IStarSettings(BaseModel):
    """Kissell's I-Star parameters: ``I = a1 (Q/ADV)^a2 sigma^a3`` bps, with
    a temporary part ``b1 I pov^a4`` and a permanent part ``(1 - b1) I``.
    The defaults are Kissell's published US-equity estimates, **not
    calibrated** for this system; treat them as a starting point."""

    model_config = ConfigDict(frozen=True)

    a1: float = Field(708.0, ge=0.0)
    a2: float = Field(0.55, gt=0.0)
    a3: float = Field(0.71, ge=0.0)
    a4: float = Field(0.5, ge=0.0)
    b1: float = Field(0.98, ge=0.0, le=1.0)
    #: Assumed participation rate of the execution (percent of volume).
    pov: float = Field(0.10, gt=0.0, le=1.0)
    #: Bars per year, to annualise ``sigma_daily`` when the trade does not
    #: carry its own (RS-19: the broker fills it per interval and class).
    periods_per_year: float = Field(252.0, gt=0.0)


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
    impact_model: ImpactModel = "sqrt"
    #: ``sqrt_vol``: impact of trading one ADV, in daily sigmas.
    impact_gamma: float = Field(1.0, ge=0.0)
    istar: IStarSettings = IStarSettings()
    #: Rates of the commission schedules and regulatory fees (23.2).
    commissions: CommissionSettings = CommissionSettings()
    half_spread_model: HalfSpreadModel = "class"
    #: Cap on a per-ticker half-spread estimate, in bps.
    max_half_spread_bps: float = Field(200.0, ge=0.0)
    #: Lagged-statistics windows, in bars (see ``MarketStatsSpec``).
    adv_window: int = Field(20, ge=1)
    vol_window: int = Field(20, ge=2)
    spread_window: int = Field(20, ge=1)

    @model_validator(mode="after")
    def _sell_prices_stay_positive(self) -> CostModelSettings:
        for costs in (self.default, *self.asset_classes.values()):
            half_spread = costs.half_spread_bps
            if self.half_spread_model != "class":
                half_spread = max(half_spread, self.max_half_spread_bps)
            if half_spread + self.max_impact_bps >= _BPS:
                raise ValueError(
                    "half_spread_bps (or max_half_spread_bps) + max_impact_bps must be "
                    "below 10000 or sell fills would be at a non-positive price"
                )
        return self

    def for_asset_class(self, asset_class: AssetClass) -> AssetClassCosts:
        return self.asset_classes.get(asset_class, self.default)

    def one_way_cost_bps(self, asset_class: AssetClass, notional: float | None = None) -> float:
        """Cost of one small trade in bps of its notional: the class's
        half-spread plus ``fee_bps``, plus the flat fee over ``notional``
        when one is given. Impact is left out (it depends on size). Used for
        a rule's cost in Sharpe units (roadmap 22.7)."""
        costs = self.for_asset_class(asset_class)
        bps = costs.half_spread_bps + costs.fee_bps
        if notional is not None and notional > 0:
            bps += costs.fee_flat / notional * _BPS
        return bps

    def market_stats_spec(self) -> MarketStatsSpec | None:
        """The lagged statistics these settings read; ``None`` for the
        legacy ``sqrt`` + ``class`` combination, which reads none."""
        if self.impact_model == "sqrt" and self.half_spread_model == "class":
            return None
        return MarketStatsSpec(
            adv_window=self.adv_window,
            vol_window=self.vol_window,
            spread_window=self.spread_window,
            spread_estimator=None if self.half_spread_model == "class" else self.half_spread_model,
        )

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

    @classmethod
    def ibkr(cls, schedule: Literal["tiered", "fixed"] = "tiered") -> CostModelSettings:
        """``realistic()`` with IBKR Pro commissions and the US regulatory
        fees on equities in place of the flat ``fee_bps`` (roadmap 23.2).
        Other asset classes keep their ``realistic()`` fees."""
        base = cls.realistic()
        equity = base.for_asset_class("equity").model_copy(
            update={"fee_bps": 0.0, "commission": f"ibkr_{schedule}", "us_sell_fees": True}
        )
        return base.model_copy(update={"asset_classes": {**base.asset_classes, "equity": equity}})


class AssetClassCostModel:
    """Per-asset-class fee and half-spread (or a per-ticker estimate) plus
    market impact (square root, volatility-scaled square root or I-Star)."""

    def __init__(self, settings: CostModelSettings) -> None:
        self._settings = settings
        self._spec = settings.market_stats_spec()

    @property
    def market_stats_spec(self) -> MarketStatsSpec | None:
        return self._spec

    def cost(self, trade: Trade) -> TradeCost:
        costs = self._settings.for_asset_class(trade.asset_class)
        temporary, permanent = self.impact_components(trade)
        adverse_bps = self._half_spread_bps(trade, costs) + temporary + permanent
        fill_price = _adverse(trade.price, trade.side, adverse_bps)
        fee = costs.fee_flat + costs.fee_bps / _BPS * fill_price * trade.quantity
        rates = self._settings.commissions
        if costs.commission != "none":
            fee += commission_fee(costs.commission, trade, fill_price, rates)
        if costs.us_sell_fees:
            fee += regulatory_fee(trade, fill_price, rates.us_regulatory)
        return TradeCost(fill_price=fill_price, fee=fee)

    def impact_components(self, trade: Trade) -> tuple[float, float]:
        """``(temporary, permanent)`` impact in bps, capped together at
        ``max_impact_bps``. The square-root models are all temporary."""
        s = self._settings
        adv, sigma = _known(trade.adv), _known(trade.sigma_daily)
        if s.impact_model == "sqrt" or adv is None or sigma is None:
            return self._sqrt_bps(trade), 0.0
        if adv <= 0:
            return s.max_impact_bps, 0.0
        size = trade.quantity / adv
        if not math.isfinite(size):  # an ADV too small to divide by (BL-49)
            return s.max_impact_bps, 0.0
        if s.impact_model == "sqrt_vol":
            return min(s.impact_gamma * sigma * _BPS * math.sqrt(size), s.max_impact_bps), 0.0
        p = s.istar
        periods = trade.periods_per_year or p.periods_per_year
        sigma_annual = sigma * math.sqrt(periods)
        i_star = p.a1 * size**p.a2 * sigma_annual**p.a3
        temporary = p.b1 * i_star * p.pov**p.a4
        permanent = (1.0 - p.b1) * i_star
        total = temporary + permanent
        if not math.isfinite(total):
            return s.max_impact_bps * p.b1, s.max_impact_bps * (1.0 - p.b1)
        if total > s.max_impact_bps:
            scale = s.max_impact_bps / total
            return temporary * scale, permanent * scale
        return temporary, permanent

    def _sqrt_bps(self, trade: Trade) -> float:
        s = self._settings
        volume = trade.bar_volume
        if s.impact_bps == 0.0 or volume is None or math.isnan(volume):
            return 0.0
        if volume <= 0:
            return s.max_impact_bps
        return min(s.impact_bps * math.sqrt(trade.quantity / volume), s.max_impact_bps)

    def _half_spread_bps(self, trade: Trade, costs: AssetClassCosts) -> float:
        floor = costs.half_spread_bps
        estimate = _known(trade.half_spread_bps)
        if self._settings.half_spread_model == "class" or estimate is None:
            return floor
        return max(floor, min(estimate, self._settings.max_half_spread_bps))


def _known(value: float | None) -> float | None:
    return None if value is None or math.isnan(value) else value
