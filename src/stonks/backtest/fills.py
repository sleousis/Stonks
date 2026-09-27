"""Fill models for the ``SimulatedBroker`` (BL-30).

``FillModel`` is the seam that decides, for one order in one bar, **how
much** fills and at **what reference price** (before the ``CostModel``
adds spread, impact and fees). The broker asks it once per order per bar;
the engine re-queues whatever it says to carry.

Market-on-open
--------------
The engine fills an order decided at bar ``t``'s close in the next bar
``t+1`` the ticker trades in. A market order's reference price is that
bar's **open**: a market-on-open order. Limit and stop orders are checked
against the same bar's open / high / low.

Implementations
---------------
- ``ImmediateFillModel`` (default, the legacy behaviour): every order fills
  in full at the open; ``order_type`` and ``limit_price`` are ignored and
  nothing carries.
- ``BarFillModel`` (``FillModelSettings.build()``):

  * **gap guard** — no fill when the bar is more than ``max_gap_days``
    calendar days after the order was decided (a halt or a delisting); the
    order expires;
  * **order types** (``honour_limits``) — a buy limit ``L`` fills if
    ``low <= L`` at ``min(open, L)``; a sell limit if ``high >= L`` at
    ``max(open, L)``; a buy stop ``S`` triggers if ``high >= S`` and fills
    at ``max(open, S)`` (a gap through the stop fills at the open); a sell
    stop triggers if ``low <= S`` and fills at ``min(open, S)``. A
    stop-limit triggers like a stop and then fills only if that price is
    within its limit. Orders are DAY orders: an untouched limit or
    untriggered stop expires. A missing high / low is taken as the open;
  * **participation cap** — at most ``max_participation`` of the bar's
    volume (``participation_basis="bar_volume"``, falling back to the
    lagged ADV when the bar has no volume) or of the lagged ADV
    (``"adv"``). With no volume and no ADV the order is not capped. The
    rest carries to the ticker's next bar when ``carry_unfilled``;
  * **zero volume** — a bar with zero volume fills nothing (unless
    ``allow_zero_volume``); the whole order carries when
    ``carry_unfilled``.

Stop price: ``Order`` has no stop-price field yet, so the model reads a
duck-typed ``order.stop_price`` and, for ``order_type="stop"`` without one,
takes ``limit_price`` as the stop trigger. A stop-limit without
``stop_price`` never fills.

Market statistics
-----------------
``MarketStats`` are per ticker and bar, **lagged one bar**: the values used
to fill in bar ``t`` come from bars ``<= t-1`` only (``adv`` is the median
volume of the prior ``adv_window`` bars; ``sigma_daily`` the std of the
prior ``vol_window`` log returns of the adjusted close, per bar;
``half_spread_bps`` an OHLC spread estimate over the prior
``spread_window`` pairs of bars). ``lagged_market_stats`` computes them,
vectorised, once per backtest; models declare what they need through
``market_stats_spec``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from stonks.core.types import Order
from stonks.features.spread import abdi_ranaldo, corwin_schultz, half_spread_bps

SpreadEstimator = Literal["corwin_schultz", "abdi_ranaldo"]

#: Relative remainder below which a carry is float dust, not an order.
_DUST = 1e-9


# ---- market statistics ------------------------------------------------------------


@dataclass(frozen=True)
class MarketStats:
    """Lagged per-ticker statistics for pricing a fill (``None`` = unknown)."""

    adv: float | None = None
    sigma_daily: float | None = None
    half_spread_bps: float | None = None


class MarketStatsSpec(BaseModel):
    """Which lagged statistics a model needs, and over how many bars."""

    model_config = ConfigDict(frozen=True)

    adv_window: int = Field(20, ge=1)
    vol_window: int = Field(20, ge=2)
    #: Pairs of bars in the spread estimate (``spread_window + 1`` bars).
    spread_window: int = Field(20, ge=1)
    #: ``None`` skips the spread estimate.
    spread_estimator: SpreadEstimator | None = None

    @property
    def lookback_bars(self) -> int:
        """Bars before the first fill bar needed to fill every window."""
        spread = self.spread_window + 1 if self.spread_estimator else 0
        return max(self.adv_window, self.vol_window + 1, spread)

    def merge(self, other: MarketStatsSpec | None) -> MarketStatsSpec:
        if other is None:
            return self
        return MarketStatsSpec(
            adv_window=max(self.adv_window, other.adv_window),
            vol_window=max(self.vol_window, other.vol_window),
            spread_window=max(self.spread_window, other.spread_window),
            spread_estimator=self.spread_estimator or other.spread_estimator,
        )


def lagged_market_stats(bars: pd.DataFrame, spec: MarketStatsSpec) -> pd.DataFrame:
    """``adv``, ``sigma_daily`` and ``half_spread_bps`` for every row of
    ``bars`` (columns ``ticker, timestamp, high, low, close, volume`` and
    optionally ``adj_close``), each from that ticker's **previous** bars
    only. Returned in ``bars``' row order; NaN where a window isn't full.

    High / low / close are rescaled by ``adj_close / close`` and volume by
    its inverse, so a split inside the window doesn't read as volatility,
    spread or a drop in volume (RS-20)."""
    out = pd.DataFrame(index=bars.index, columns=["adv", "sigma_daily", "half_spread_bps"])
    if bars.empty:
        return out.astype(float)
    frame = bars.sort_values(["ticker", "timestamp"], kind="stable")
    close = frame["close"].astype(float)
    adj = frame["adj_close"].astype(float) if "adj_close" in frame else close
    factor = (adj / close).where(close > 0).fillna(1.0)
    high = frame["high"].astype(float).fillna(close) * factor
    low = frame["low"].astype(float).fillna(close) * factor
    adj_close = close * factor
    volume = frame["volume"].astype(float) / factor
    tickers = frame["ticker"]

    def per_ticker(series: pd.Series, fn) -> pd.Series:
        return series.groupby(tickers, sort=False).transform(fn)

    adv = per_ticker(volume, lambda v: v.rolling(spec.adv_window, spec.adv_window).median())
    returns = per_ticker(adj_close, lambda c: np.log(c).diff())
    sigma = per_ticker(returns, lambda r: r.rolling(spec.vol_window, spec.vol_window).std(ddof=1))
    result = pd.DataFrame({"adv": adv, "sigma_daily": sigma}, index=frame.index)
    if spec.spread_estimator is not None:
        estimator = corwin_schultz if spec.spread_estimator == "corwin_schultz" else abdi_ranaldo
        parts = []
        for idx in frame.groupby("ticker", sort=False).groups.values():
            parts.append(
                half_spread_bps(
                    estimator(high.loc[idx], low.loc[idx], adj_close.loc[idx], spec.spread_window)
                )
            )
        result["half_spread_bps"] = pd.concat(parts) if parts else np.nan
    else:
        result["half_spread_bps"] = np.nan
    lagged = result.groupby(tickers, sort=False, group_keys=False).shift(1)
    return lagged.reindex(bars.index).astype(float)


def _known(value: float | None) -> float | None:
    return None if value is None or math.isnan(value) else float(value)


def market_stats_from_row(adv, sigma_daily, half_spread) -> MarketStats:
    """A ``MarketStats`` from possibly-NaN scalars."""
    return MarketStats(_known(adv), _known(sigma_daily), _known(half_spread))


# ---- the seam -------------------------------------------------------------------------


@dataclass(frozen=True)
class BarQuote:
    """What the broker knows about the bar an order may fill in."""

    open: float
    high: float | None = None
    low: float | None = None
    #: Units traded in the bar; ``None`` when unknown.
    volume: float | None = None
    #: Lagged average daily volume (see ``MarketStats``).
    adv: float | None = None
    #: Calendar days from the order's decision to this bar; ``None`` unknown.
    gap_days: float | None = None
    #: Length of one bar of the backtest interval in days; ``None`` unknown.
    bar_days: float | None = None


@dataclass(frozen=True)
class FillDecision:
    """``quantity`` fills now at reference ``price``; ``carry`` re-queues."""

    quantity: float
    price: float | None
    carry: float = 0.0
    reason: str | None = None

    @classmethod
    def none(cls, reason: str, carry: float = 0.0) -> FillDecision:
        return cls(quantity=0.0, price=None, carry=carry, reason=reason)


@runtime_checkable
class FillModel(Protocol):
    def decide(self, order: Order, quote: BarQuote) -> FillDecision: ...


class ImmediateFillModel:
    """Legacy: the whole order at the bar's open, whatever its type."""

    market_stats_spec: MarketStatsSpec | None = None

    def decide(self, order: Order, quote: BarQuote) -> FillDecision:
        return FillDecision(quantity=order.quantity, price=quote.open)


class FillModelSettings(BaseModel):
    """Settings for ``BarFillModel``; see the module docstring."""

    model_config = ConfigDict(frozen=True)

    #: Largest fraction of the basis volume one order may take per bar;
    #: ``None`` disables the cap.
    max_participation: float | None = Field(0.10, gt=0.0, le=1.0)
    participation_basis: Literal["bar_volume", "adv"] = "bar_volume"
    carry_unfilled: bool = True
    allow_zero_volume: bool = False
    honour_limits: bool = True
    #: Longest decision-to-fill gap, in calendar days; ``None`` disables.
    #: Never shorter than two bars of the backtest interval (RS-14), so
    #: weekly, monthly and yearly bars still fill.
    max_gap_days: float | None = Field(7.0, gt=0.0)
    adv_window: int = Field(20, ge=1)

    def build(self) -> BarFillModel:
        return BarFillModel(self)


class BarFillModel:
    """Participation cap, limit and stop orders, gap guard (BL-30)."""

    def __init__(self, settings: FillModelSettings | None = None) -> None:
        self._s = settings or FillModelSettings()

    @property
    def settings(self) -> FillModelSettings:
        return self._s

    @property
    def market_stats_spec(self) -> MarketStatsSpec:
        return MarketStatsSpec(adv_window=self._s.adv_window)

    def decide(self, order: Order, quote: BarQuote) -> FillDecision:
        s = self._s
        gap = quote.gap_days
        if s.max_gap_days is not None and gap is not None:
            limit = max(s.max_gap_days, 2.0 * (quote.bar_days or 0.0))
            if gap > limit:
                return FillDecision.none("gap")
        price, reason = self._price(order, quote) if s.honour_limits else (quote.open, None)
        if price is None:
            return FillDecision.none(reason or "not_triggered")  # DAY order expires
        carry_all = order.quantity if s.carry_unfilled else 0.0
        if quote.volume is not None and quote.volume <= 0 and not s.allow_zero_volume:
            return FillDecision.none("zero_volume", carry=carry_all)
        cap = self._cap(quote)
        if cap is None or cap >= order.quantity:
            return FillDecision(quantity=order.quantity, price=price)
        if cap <= 0:
            return FillDecision.none("participation", carry=carry_all)
        rest = order.quantity - cap if s.carry_unfilled else 0.0
        if rest <= _DUST * order.quantity:
            rest = 0.0
        return FillDecision(quantity=cap, price=price, carry=rest, reason="participation")

    def _cap(self, quote: BarQuote) -> float | None:
        rho = self._s.max_participation
        if rho is None:
            return None
        volume = quote.volume if quote.volume is not None and quote.volume > 0 else None
        basis = quote.adv if self._s.participation_basis == "adv" else (volume or quote.adv)
        return None if basis is None else rho * basis

    @staticmethod
    def _price(order: Order, quote: BarQuote) -> tuple[float | None, str | None]:
        o = quote.open
        high = quote.high if quote.high is not None else o
        low = quote.low if quote.low is not None else o
        buy = order.side == "buy"
        kind = order.order_type
        if kind == "market":
            return o, None
        if kind == "limit":
            return _limit(buy, o, high, low, order.limit_price)
        stop = getattr(order, "stop_price", None)
        if stop is None and kind == "stop":
            stop = order.limit_price
        if stop is None:
            return None, "missing_stop_price"
        if (buy and high < stop) or (not buy and low > stop):
            return None, "stop_not_triggered"
        triggered = max(o, stop) if buy else min(o, stop)
        if kind == "stop":
            return triggered, None
        limit = order.limit_price
        if limit is None or (triggered > limit if buy else triggered < limit):
            return None, "limit_not_reached"
        return triggered, None


def _limit(
    buy: bool, o: float, high: float, low: float, limit: float | None
) -> tuple[float | None, str | None]:
    if limit is None:
        return None, "missing_limit_price"
    if buy:
        return (min(o, limit), None) if low <= limit else (None, "limit_not_reached")
    return (max(o, limit), None) if high >= limit else (None, "limit_not_reached")


# ---- bundle for config -------------------------------------------------------------


class ExecutionSettings(BaseModel):
    """Execution realism for a simulated broker: the fill model (``None`` =
    the legacy immediate fill) and cash-account settlement (``0`` = sale
    proceeds are available at once; ``1`` = T+1 business days)."""

    model_config = ConfigDict(frozen=True)

    fill: FillModelSettings | None = None
    settlement_days: int = Field(0, ge=0, le=5)

    def fill_model(self) -> FillModel:
        return self.fill.build() if self.fill is not None else ImmediateFillModel()
