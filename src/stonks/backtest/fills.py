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

- ``MinuteFillModel`` (``MinuteFillSettings.build()``, roadmap 21.2.3):
  the fills of an intraday book. It uses ``BarFillModel`` for order types
  and the participation cap of the minute's volume, and adds:

  * **day orders**: an order that is not good till cancelled expires
    when its fill bar is in a later session than its decision
    (``BarQuote.new_session``). An IOC order never carries.
  * **gap in bars**: the gap guard counts bars (``BarQuote.gap_bars``),
    not calendar days.
  * **half spread**: with a recorded quote (``bid`` and ``ask``) a buy
    pays half the spread above the reference price and a sell half below,
    never past a limit. Without a quote nothing is added, and the cost
    model's class half spread applies as in the daily path. With recorded
    quotes, set the cost model's class half spread to zero, or the spread
    is paid twice.

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
    spread or a drop in volume (RS-20). ADV is then expressed in the fill
    bar's raw shares, the units of the order it caps and prices."""
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
    # ADV is in the shares of the last adjusted bar; express it in the fill
    # bar's own shares, which the order quantity is in. Only the splits
    # between the window and the fill bar remain, so no later split leaks in.
    lagged["adv"] = lagged["adv"].astype(float) * factor
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
    # ---- intraday fields (roadmap 21.2.3). The daily model ignores them. ----
    #: A recorded quote at the fill bar's open (``None`` when unknown).
    bid: float | None = None
    ask: float | None = None
    #: Bars from the decision bar's close to this bar's start (0 = the next bar).
    gap_bars: float | None = None
    #: Whether this bar is in a later session than the decision.
    new_session: bool | None = None


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


class MinuteFillSettings(BaseModel):
    """Settings for ``MinuteFillModel``. See the module docstring."""

    model_config = ConfigDict(frozen=True)

    #: Largest fraction of the minute's volume one order takes per bar.
    max_participation: float | None = Field(0.10, gt=0.0, le=1.0)
    carry_unfilled: bool = True
    allow_zero_volume: bool = False
    honour_limits: bool = True
    #: Most bars between the decision and the fill bar (``None`` disables).
    max_gap_bars: int | None = Field(5, ge=0)
    #: Day orders expire when the fill bar is in a later session.
    expire_at_session_end: bool = True
    #: Add the half spread of a recorded quote to the reference price.
    use_quote_spread: bool = True
    adv_window: int = Field(20, ge=1)

    def build(self) -> MinuteFillModel:
        return MinuteFillModel(self)


class MinuteFillModel:
    """Next-bar fills on minute bars: day orders, a gap in bars, the half
    spread of a recorded quote, over ``BarFillModel`` (roadmap 21.2.3)."""

    def __init__(self, settings: MinuteFillSettings | None = None) -> None:
        self._s = settings or MinuteFillSettings.model_validate({})
        self._bars = BarFillModel(
            FillModelSettings(
                max_participation=self._s.max_participation,
                participation_basis="bar_volume",
                carry_unfilled=self._s.carry_unfilled,
                allow_zero_volume=self._s.allow_zero_volume,
                honour_limits=self._s.honour_limits,
                max_gap_days=None,
                adv_window=self._s.adv_window,
            )
        )

    @property
    def settings(self) -> MinuteFillSettings:
        return self._s

    @property
    def market_stats_spec(self) -> MarketStatsSpec:
        return MarketStatsSpec.model_validate({"adv_window": self._s.adv_window})

    def decide(self, order: Order, quote: BarQuote) -> FillDecision:
        s = self._s
        day_order = order.time_in_force != "gtc"
        if s.expire_at_session_end and day_order and quote.new_session:
            return FillDecision.none("session_end")
        gap = quote.gap_bars
        if s.max_gap_bars is not None and gap is not None and gap > s.max_gap_bars:
            return FillDecision.none("gap")
        decision = self._bars.decide(order, quote)
        if order.time_in_force == "ioc" and decision.carry:
            decision = FillDecision(decision.quantity, decision.price, 0.0, decision.reason)
        if decision.price is None or not s.use_quote_spread:
            return decision
        price = _with_half_spread(order, decision.price, quote.bid, quote.ask)
        return FillDecision(decision.quantity, price, decision.carry, decision.reason)


def _with_half_spread(order: Order, price: float, bid: float | None, ask: float | None) -> float:
    """``price`` plus half the quoted spread against the order, never past
    its limit. A missing or crossed quote adds nothing."""
    if bid is None or ask is None or bid <= 0 or ask < bid:
        return price
    half = (ask - bid) / 2.0
    buy = order.side == "buy"
    adjusted = price + half if buy else price - half
    limit = order.limit_price if order.order_type in ("limit", "stop_limit") else None
    if limit is not None:
        adjusted = min(adjusted, limit) if buy else max(adjusted, limit)
    return adjusted


def triggered_price(
    order: Order, open_: float, high: float | None = None, low: float | None = None
) -> float | None:
    """The price a stop (or stop-limit) ``order`` fills at in a bar with this
    open, high and low, or ``None`` when the bar does not reach it. A gap
    through the stop fills at the open. The resting protective stops of a
    simulated book use it (roadmap 19.10)."""
    price, _ = BarFillModel._price(order, BarQuote(open=open_, high=high, low=low))
    return price


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
