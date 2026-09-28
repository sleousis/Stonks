"""The tax side of a trade before it is placed, and the tax owed so far this
year (roadmap 23.5). Pure. Not tax advice: an estimate at the rates you set.

A preview replays the book's fills with the proposed trade added last,
through the same lot rules as the exports (:mod:`stonks.tax.lots`): FIFO,
or your picks first with ``lot_method = "specific"``. It shows the lots the
trade closes, their holding periods, the realised gain, a US wash sale
warning, and the estimated tax at ``[tax.rates]``.

The yearly figure nets gains and losses within each term, then a net loss
in one term against a gain in the other, before applying each term's rate.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from stonks.tax.lots import (
    WASH_WINDOW,
    Disposal,
    HoldingPeriod,
    TaxFill,
    TaxSettings,
    TaxSplit,
    realized_disposals,
)
from stonks.tax.settings import TaxRates, TaxRatesConfig

__all__ = ["PreviewLot", "TaxPreview", "YearTax", "preview_trade", "year_tax"]

_EPS = 1e-9
Picks = Mapping[int, Sequence[tuple[int, float]]]
#: ``(amount, currency, day) -> amount in the base currency``, or ``None``
#: when no rate converts it.
ToBase = Callable[[float, str | None, date], float | None]


@dataclass(frozen=True)
class PreviewLot:
    """One lot (or part of one) the trade closes."""

    open_fill_id: int
    kind: str
    quantity: float
    acquired: date
    holding_period: HoldingPeriod
    cost_basis: float
    proceeds: float
    gain: float
    wash_sale_disallowed: float


@dataclass(frozen=True)
class TaxPreview:
    ticker: str
    side: str
    quantity: float
    price: float
    currency: str | None
    lots: tuple[PreviewLot, ...]
    proceeds: float
    realized_gain: float
    short_term_gain: float
    long_term_gain: float
    wash_sale_disallowed: float
    #: The tax on this trade's own gains (0 on a loss).
    estimated_tax: float
    #: The proceeds of a sell less its estimated tax (0 for a buy).
    after_tax_proceeds: float
    #: How much this trade changes the year's estimated tax (negative: a
    #: loss that lowers it).
    year_tax_change: float
    wash_sale_warning: str | None
    rates: TaxRates


@dataclass(frozen=True)
class YearTax:
    year: int
    short_term_gain: float
    long_term_gain: float
    wash_sale_disallowed: float
    estimated_tax: float
    disposals: int
    rates: TaxRates
    #: Disposals left out because no FX rate converts their gain.
    unconverted: int = 0


def _tax(short: float, long: float, rates: TaxRates) -> float:
    if short < 0 < long:
        long, short = long + short, 0.0
    elif long < 0 < short:
        short, long = short + long, 0.0
    return max(short, 0.0) * rates.short_term + max(long, 0.0) * rates.long_term


def _split(
    disposals: Iterable[Disposal], to_base: ToBase | None = None
) -> tuple[float, float, float, int, int]:
    """``(short gain, long gain, wash disallowed, disposals, unconverted)``."""
    short = long = wash = 0.0
    n = missing = 0
    for d in disposals:
        gain, disallowed = d.gain, d.wash_sale_disallowed
        if to_base is not None:
            # like the gains export: each amount at the rate of its own day
            p = to_base(d.proceeds, d.currency, d.sale_day)
            c = to_base(d.cost_basis, d.currency, d.purchase_day)
            w = to_base(disallowed, d.currency, d.disposed)
            if p is None or c is None or w is None:
                missing += 1
                continue
            gain, disallowed = p - c + w, w
        n += 1
        wash += disallowed
        if d.holding_period == "long":
            long += gain
        else:
            short += gain
    return short, long, wash, n, missing


def year_tax(
    fills: Iterable[TaxFill],
    settings: TaxSettings,
    rates: TaxRatesConfig,
    *,
    year: int,
    picks: Picks | None = None,
    splits: Iterable[TaxSplit] = (),
    as_of: date | None = None,
    to_base: ToBase | None = None,
) -> YearTax:
    """The gains realised in ``year`` (up to ``as_of``) and their estimated
    tax, in the base currency when ``to_base`` converts them."""
    upto = [f for f in fills if as_of is None or f.day <= as_of]
    disposals = [
        d for d in realized_disposals(upto, settings, picks, splits) if d.disposed.year == year
    ]
    short, long, wash, n, missing = _split(disposals, to_base)
    chosen = rates.for_jurisdiction(settings.jurisdiction)
    return YearTax(
        year=year,
        short_term_gain=short,
        long_term_gain=long,
        wash_sale_disallowed=wash,
        estimated_tax=_tax(short, long, chosen),
        disposals=n,
        rates=chosen,
        unconverted=missing,
    )


def preview_trade(
    fills: Sequence[TaxFill],
    settings: TaxSettings,
    rates: TaxRatesConfig,
    *,
    ticker: str,
    side: str,
    quantity: float,
    price: float,
    when: datetime,
    fee: float = 0.0,
    currency: str | None = None,
    picks: Sequence[tuple[int, float]] = (),
    past_picks: Picks | None = None,
    splits: Iterable[TaxSplit] = (),
) -> TaxPreview:
    """What a ``side`` of ``quantity`` ``ticker`` at ``price`` would realise
    now. ``picks`` are the lots this sell closes first (specific lots);
    ``past_picks`` are the stored picks of earlier sells."""
    split_list = list(splits)
    trade_id = max((f.id for f in fills), default=0) + 1
    trade = TaxFill(
        id=trade_id,
        ticker=ticker,
        side=side,  # type: ignore[arg-type]
        quantity=quantity,
        price=price,
        fee=fee,
        filled_at=when,
        currency=currency,
    )
    all_picks: dict[int, Sequence[tuple[int, float]]] = dict(past_picks or {})
    if picks:
        all_picks[trade_id] = list(picks)
    before = [f for f in fills if f.filled_at <= when]
    after = [*before, trade]
    disposals = realized_disposals(after, settings, all_picks, split_list)
    mine = [d for d in disposals if d.close_fill_id == trade_id]
    short, long, wash, _, _ = _split(mine)
    chosen = rates.for_jurisdiction(settings.jurisdiction)
    own_tax = _tax(max(short, 0.0), max(long, 0.0), chosen)
    year = when.year
    with_trade = year_tax(after, settings, rates, year=year, picks=all_picks, splits=split_list)
    without = year_tax(before, settings, rates, year=year, picks=all_picks, splits=split_list)
    proceeds = quantity * price - fee if side == "sell" else 0.0
    lots = tuple(
        PreviewLot(
            open_fill_id=d.open_fill_id,
            kind=d.kind,
            quantity=d.quantity,
            acquired=d.acquired,
            holding_period=d.holding_period,
            cost_basis=d.cost_basis,
            proceeds=d.proceeds,
            gain=d.gain,
            wash_sale_disallowed=d.wash_sale_disallowed,
        )
        for d in mine
    )
    return TaxPreview(
        ticker=ticker,
        side=side,
        quantity=quantity,
        price=price,
        currency=currency,
        lots=lots,
        proceeds=proceeds,
        realized_gain=short + long,
        short_term_gain=short,
        long_term_gain=long,
        wash_sale_disallowed=wash,
        estimated_tax=own_tax,
        after_tax_proceeds=proceeds - own_tax if side == "sell" else 0.0,
        year_tax_change=with_trade.estimated_tax - without.estimated_tax,
        wash_sale_warning=_wash_warning(settings, mine, disposals, trade, wash),
        rates=chosen,
    )


def _wash_warning(
    settings: TaxSettings,
    mine: Sequence[Disposal],
    disposals: Sequence[Disposal],
    trade: TaxFill,
    disallowed: float,
) -> str | None:
    if not settings.applies_wash_sales:
        return None
    days = WASH_WINDOW.days
    if disallowed > _EPS:
        return (
            f"wash sale: {trade.ticker} was bought within {days} days, so {disallowed:,.2f} "
            "of this loss is disallowed and moves to the basis of those shares"
        )
    loss = sum(d.proceeds - d.cost_basis for d in mine if d.kind == "long")
    if trade.side == "sell" and loss < -_EPS:
        return (
            f"a loss of {-loss:,.2f}: a buy of {trade.ticker} within {days} days after "
            "this sale would disallow it (US wash sale)"
        )
    if trade.side == "buy":
        recent = [
            d
            for d in disposals
            if d.ticker == trade.ticker
            and d.kind == "long"
            and d.close_fill_id != trade.id
            and d.proceeds - d.cost_basis < -_EPS
            and 0 <= (trade.day - d.disposed).days <= days
        ]
        if recent:
            last = max(d.disposed for d in recent)
            return (
                f"you sold {trade.ticker} at a loss on {last.isoformat()}: this buy within "
                f"{days} days would disallow that loss (US wash sale)"
            )
    return None
