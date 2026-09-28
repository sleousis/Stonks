"""Broker commission schedules and US regulatory fees (roadmap 23.2).

A cost model option: an asset class in ``[backtest.costs.asset_classes.*]``
names its ``commission`` schedule and whether it pays ``us_sell_fees``.
Both default off, so existing costs do not change. The fee of a fill is
then ``fee_flat + fee_bps`` (as before) plus the schedule's commission plus
the regulatory fees. The lab, the paper books and TCA's expected cost all
price through the same :class:`~stonks.backtest.costs.AssetClassCostModel`,
so all three see the same fees.

Schedules (a registry, :data:`COMMISSIONS`; a new one is one function):

- ``none``: no commission (the default).
- ``ibkr_fixed``: IBKR Pro Fixed for US stocks. A rate per share with a
  minimum per order and a cap of a percent of the trade value. Exchange
  and clearing fees are included in the rate.
- ``ibkr_tiered``: IBKR Pro Tiered. A per-share rate that falls with the
  month's volume (``monthly_shares`` picks the tier), the same minimum and
  cap, plus exchange and clearing fees per share on top.

When the percent cap is below the minimum, the cap wins, as IBKR's pricing
page states. The simulator charges per fill, so an order that fills over
several bars pays the minimum on each part.

US regulatory fees (:class:`UsRegulatoryFees`):

- SEC Section 31 fee on the value of a sale;
- FINRA Trading Activity Fee (TAF) per share sold, capped per trade;
- the Consolidated Audit Trail (CAT) fee per share, on buys and sells.

Every rate is a setting. The defaults are the published rates when this was
written, with the source next to each. Check them against the sources
before trusting a figure: SEC and FINRA change their rates each year.

Contract (from :mod:`stonks.backtest.costs`): every fee here is
non-decreasing in quantity.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from stonks.backtest.costs import Trade


class IbkrFixedFees(BaseModel):
    """IBKR Pro Fixed, US stocks and ETFs.
    Source: interactivebrokers.com/en/pricing/commissions-stocks.php
    (Fixed: USD 0.005 per share, minimum USD 1.00, maximum 1% of trade value)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    per_share: float = Field(0.005, ge=0.0)
    minimum: float = Field(1.00, ge=0.0)
    #: The cap as a fraction of the trade value (0.01 = 1%).
    max_fraction: float = Field(0.01, gt=0.0)


#: IBKR Pro Tiered, US stocks: (monthly shares up to, USD per share).
#: Source: interactivebrokers.com/en/pricing/commissions-stocks.php (Tiered).
IBKR_TIERS: tuple[tuple[float, float], ...] = (
    (300_000, 0.0035),
    (3_000_000, 0.0020),
    (20_000_000, 0.0015),
    (100_000_000, 0.0010),
    (float("inf"), 0.0005),
)


class IbkrTieredFees(BaseModel):
    """IBKR Pro Tiered, US stocks and ETFs. Source: the page above
    (minimum USD 0.35 per order, maximum 1% of trade value, plus exchange,
    clearing and pass-through fees)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tiers: tuple[tuple[float, float], ...] = IBKR_TIERS
    #: Shares traded this month, which picks the tier (0 = the first tier).
    monthly_shares: float = Field(0.0, ge=0.0)
    minimum: float = Field(0.35, ge=0.0)
    max_fraction: float = Field(0.01, gt=0.0)
    #: Exchange fee per share. 0.003 is a typical fee to take liquidity on a
    #: US lit venue (Reg NMS caps it at 0.003). Adding liquidity earns a
    #: rebate instead, so this is a cautious figure.
    exchange_per_share: float = Field(0.003, ge=0.0)
    #: Clearing fee per share (NSCC and DTC, USD 0.00020 on IBKR's page).
    clearing_per_share: float = Field(0.0002, ge=0.0)

    @field_validator("tiers")
    @classmethod
    def _ascending(cls, tiers: tuple[tuple[float, float], ...]) -> tuple[tuple[float, float], ...]:
        if not tiers:
            raise ValueError("tiers needs at least one (up_to, per_share) row")
        bounds = [up_to for up_to, _ in tiers]
        if bounds != sorted(bounds) or len(set(bounds)) != len(bounds):
            raise ValueError("tier bounds must ascend")
        if any(rate < 0 for _, rate in tiers):
            raise ValueError("tier rates must be >= 0")
        return tiers

    def rate(self) -> float:
        for up_to, per_share in self.tiers:
            if self.monthly_shares <= up_to:
                return per_share
        return self.tiers[-1][1]


class UsRegulatoryFees(BaseModel):
    """US regulatory transaction fees, passed through by IBKR on either plan."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: SEC Section 31 fee, USD per million of sale value. 27.80 is the fiscal
    #: 2025 rate (SEC Fee Rate Advisory, effective 2024-05-22). Source:
    #: sec.gov/divisions/marketreg/sec-fee-rate-advisories.
    sec_rate_per_million: float = Field(27.80, ge=0.0)
    #: FINRA TAF, USD per share sold, and its cap per trade (2024 rates).
    #: Source: FINRA By-Laws, Schedule A, Section 1.
    taf_per_share: float = Field(0.000166, ge=0.0)
    taf_max: float = Field(8.30, ge=0.0)
    #: CAT fee, USD per executed share, on both sides. Source: CAT LLC fee
    #: schedule (catnmsplan.com), as passed through on IBKR's pricing page.
    cat_per_share: float = Field(0.000035, ge=0.0)


class CommissionSettings(BaseModel):
    """``[backtest.costs.commissions]``: the rates of every schedule. An
    asset class picks its schedule with ``commission``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ibkr_fixed: IbkrFixedFees = IbkrFixedFees()
    ibkr_tiered: IbkrTieredFees = IbkrTieredFees()
    us_regulatory: UsRegulatoryFees = UsRegulatoryFees()


#: ``(trade, fill price, settings) -> commission in money``.
CommissionFn = Callable[["Trade", float, CommissionSettings], float]

#: Commission schedules by name. A new schedule is one decorated function.
COMMISSIONS: dict[str, CommissionFn] = {}


def commission(name: str) -> Callable[[CommissionFn], CommissionFn]:
    def register(fn: CommissionFn) -> CommissionFn:
        COMMISSIONS[name] = fn
        return fn

    return register


def _per_share(shares: float, rate: float, minimum: float, cap: float) -> float:
    """``shares x rate``, at least ``minimum``, at most ``cap`` (the cap wins)."""
    return min(max(shares * rate, minimum), cap)


@commission("none")
def _none(trade: Trade, fill_price: float, settings: CommissionSettings) -> float:
    return 0.0


@commission("ibkr_fixed")
def _ibkr_fixed(trade: Trade, fill_price: float, settings: CommissionSettings) -> float:
    s = settings.ibkr_fixed
    value = abs(trade.quantity) * fill_price
    return _per_share(abs(trade.quantity), s.per_share, s.minimum, s.max_fraction * value)


@commission("ibkr_tiered")
def _ibkr_tiered(trade: Trade, fill_price: float, settings: CommissionSettings) -> float:
    s = settings.ibkr_tiered
    shares = abs(trade.quantity)
    value = shares * fill_price
    broker = _per_share(shares, s.rate(), s.minimum, s.max_fraction * value)
    return broker + shares * (s.exchange_per_share + s.clearing_per_share)


def commission_fee(
    name: str, trade: Trade, fill_price: float, settings: CommissionSettings
) -> float:
    """The commission of ``trade`` filled at ``fill_price`` under the named
    schedule. Raises ``KeyError`` for an unknown name."""
    if trade.quantity == 0:
        return 0.0
    return COMMISSIONS[name](trade, fill_price, settings)


def regulatory_fee(trade: Trade, fill_price: float, fees: UsRegulatoryFees) -> float:
    """SEC fee and FINRA TAF on a sale, plus the CAT fee on either side."""
    shares = abs(trade.quantity)
    fee = shares * fees.cat_per_share
    if trade.side == "sell":
        fee += shares * fill_price * fees.sec_rate_per_million / 1_000_000.0
        fee += min(shares * fees.taf_per_share, fees.taf_max)
    return fee
