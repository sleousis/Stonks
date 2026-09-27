"""Realized gains per lot (roadmap 20.5). Pure: fills in, disposals out.

Rules, per ticker in fill order:

- A buy first covers open short lots (oldest first), then opens a long lot
  whose cost is its price plus its fee.
- A sell closes long lots: with ``lot_method = "specific"`` the lots the
  person picked for that sell first, then the oldest (FIFO). What is left
  opens a short lot whose proceeds are the price less the fee.
- A cover realizes the short: acquired is the short sale's day, disposed is
  the cover's day, and it is always short term.
- Holding period: long when held more than one year, else short.
- US wash sales (``wash_sales`` on, jurisdiction ``us``): a long lot sold
  at a loss with a buy of the same ticker within 30 days before or after
  (other than the lots it sold) has its loss disallowed pro rata by the
  replacement quantity. The disallowed amount is added to the replacement
  buy's cost, spread over that buy's shares. Short covers are not
  adjusted, and the holding period of the replaced lot is not carried over.
- EU and UK: no wash sale adjustment. UK share matching (same day, 30 day
  bed and breakfast, section 104 pool) is out of scope: FIFO is used.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal

Jurisdiction = Literal["us", "eu", "uk"]
LotMethod = Literal["fifo", "specific"]
HoldingPeriod = Literal["short", "long"]
LotKind = Literal["long", "short"]

WASH_WINDOW = timedelta(days=30)
_EPS = 1e-9


@dataclass(frozen=True)
class TaxFill:
    id: int
    ticker: str
    side: Literal["buy", "sell"]
    quantity: float
    price: float
    fee: float
    filled_at: datetime
    currency: str | None = None

    @property
    def day(self) -> date:
        return self.filled_at.date()


@dataclass(frozen=True)
class TaxSettings:
    jurisdiction: Jurisdiction = "us"
    lot_method: LotMethod = "fifo"
    wash_sales: bool = True

    @property
    def applies_wash_sales(self) -> bool:
        return self.jurisdiction == "us" and self.wash_sales


@dataclass(frozen=True)
class Disposal:
    ticker: str
    kind: LotKind
    quantity: float
    acquired: date
    disposed: date
    proceeds: float
    cost_basis: float
    wash_sale_disallowed: float
    currency: str | None
    #: The fill that opened the lot and the fill that closed it.
    open_fill_id: int
    close_fill_id: int

    @property
    def gain(self) -> float:
        """Proceeds less cost, with the disallowed wash sale loss added back."""
        return self.proceeds - self.cost_basis + self.wash_sale_disallowed

    @property
    def holding_period(self) -> HoldingPeriod:
        if self.kind == "short":
            return "short"
        return "long" if self.disposed > _one_year_after(self.acquired) else "short"


def _one_year_after(day: date) -> date:
    try:
        return day.replace(year=day.year + 1)
    except ValueError:  # 29 February
        return day.replace(year=day.year + 1, day=28)


@dataclass
class _Lot:
    fill_id: int
    kind: LotKind
    day: date
    quantity: float
    #: Cost per share for a long lot, proceeds per share for a short lot.
    per_share: float
    currency: str | None
    #: Wash sale basis added to this lot, not yet consumed by a sale.
    added_basis: float = 0.0


@dataclass
class _Book:
    lots: list[_Lot] = field(default_factory=list)


def realized_disposals(
    fills: Iterable[TaxFill],
    settings: TaxSettings | None = None,
    picks: Mapping[int, Sequence[tuple[int, float]]] | None = None,
) -> list[Disposal]:
    """Every realized disposal of ``fills``, in fill order.

    ``picks`` maps a sell fill id to ``(buy fill id, quantity)`` pairs it
    closes first (``lot_method = "specific"`` only)."""
    cfg = settings or TaxSettings()
    ordered = sorted(fills, key=lambda f: (f.filled_at, f.id))
    specific = picks if cfg.lot_method == "specific" and picks else {}
    books: dict[str, _Book] = {}
    out: list[Disposal] = []
    #: Wash sale basis waiting for a buy that has not happened yet.
    pending_basis: dict[int, float] = {}
    #: Replacement shares already used per buy fill.
    used_as_replacement: dict[int, float] = {}
    buys_by_ticker: dict[str, list[TaxFill]] = {}
    for f in ordered:
        if f.side == "buy":
            buys_by_ticker.setdefault(f.ticker, []).append(f)

    for f in ordered:
        book = books.setdefault(f.ticker, _Book())
        if f.side == "buy":
            remaining = f.quantity
            fee_per_share = f.fee / f.quantity if f.quantity else 0.0
            for lot in [x for x in book.lots if x.kind == "short"]:
                if remaining <= _EPS:
                    break
                qty = min(lot.quantity, remaining)
                out.append(
                    Disposal(
                        ticker=f.ticker,
                        kind="short",
                        quantity=qty,
                        acquired=lot.day,
                        disposed=f.day,
                        proceeds=lot.per_share * qty,
                        cost_basis=(f.price + fee_per_share) * qty,
                        wash_sale_disallowed=0.0,
                        currency=f.currency or lot.currency,
                        open_fill_id=lot.fill_id,
                        close_fill_id=f.id,
                    )
                )
                lot.quantity -= qty
                remaining -= qty
            book.lots = [x for x in book.lots if x.quantity > _EPS]
            if remaining > _EPS:
                added = pending_basis.pop(f.id, 0.0) * (remaining / f.quantity)
                book.lots.append(
                    _Lot(f.id, "long", f.day, remaining, f.price + fee_per_share, f.currency, added)
                )
            continue

        # a sell
        remaining = f.quantity
        fee_per_share = f.fee / f.quantity if f.quantity else 0.0
        net_price = f.price - fee_per_share
        chosen: list[tuple[_Lot, float]] = []
        longs = [x for x in book.lots if x.kind == "long"]
        for buy_id, qty in specific.get(f.id, ()):
            lot = next((x for x in longs if x.fill_id == buy_id), None)
            if lot is None or remaining <= _EPS:
                continue
            take = min(qty, lot.quantity - _taken(chosen, lot), remaining)
            if take > _EPS:
                chosen.append((lot, take))
                remaining -= take
        for lot in longs:
            if remaining <= _EPS:
                break
            take = min(lot.quantity - _taken(chosen, lot), remaining)
            if take > _EPS:
                chosen.append((lot, take))
                remaining -= take
        sold_lots = {lot.fill_id for lot, _ in chosen}
        for lot, qty in chosen:
            added = lot.added_basis * (qty / lot.quantity) if lot.quantity > _EPS else 0.0
            lot.added_basis -= added
            cost = lot.per_share * qty + added
            proceeds = net_price * qty
            disallowed = 0.0
            loss = cost - proceeds
            if loss > _EPS and cfg.applies_wash_sales:
                disallowed = _wash(
                    f,
                    qty,
                    loss,
                    buys_by_ticker.get(f.ticker, []),
                    sold_lots,
                    used_as_replacement,
                    book,
                    pending_basis,
                )
            out.append(
                Disposal(
                    ticker=f.ticker,
                    kind="long",
                    quantity=qty,
                    acquired=lot.day,
                    disposed=f.day,
                    proceeds=proceeds,
                    cost_basis=cost,
                    wash_sale_disallowed=disallowed,
                    currency=f.currency or lot.currency,
                    open_fill_id=lot.fill_id,
                    close_fill_id=f.id,
                )
            )
            lot.quantity -= qty
        book.lots = [x for x in book.lots if x.quantity > _EPS]
        if remaining > _EPS:
            book.lots.append(_Lot(f.id, "short", f.day, remaining, net_price, f.currency))
    return out


def _taken(chosen: list[tuple[_Lot, float]], lot: _Lot) -> float:
    return sum(q for x, q in chosen if x is lot)


def _wash(
    sale: TaxFill,
    quantity: float,
    loss: float,
    buys: Sequence[TaxFill],
    sold_lots: set[int],
    used: dict[int, float],
    book: _Book,
    pending: dict[int, float],
) -> float:
    """Disallow ``loss`` on ``quantity`` sold shares pro rata by the
    replacement shares bought within 30 days before or after the sale, and
    move each part to its replacement buy's basis. Returns the disallowed
    amount."""
    need = quantity
    disallowed = 0.0
    for buy in buys:
        if need <= _EPS:
            break
        if buy.id in sold_lots or buy.id == sale.id:
            continue
        if abs((buy.day - sale.day).days) > WASH_WINDOW.days:
            continue
        if buy.filled_at == sale.filled_at and buy.id < sale.id:
            continue
        free = buy.quantity - used.get(buy.id, 0.0)
        take = min(free, need)
        if take <= _EPS:
            continue
        part = loss * (take / quantity)
        used[buy.id] = used.get(buy.id, 0.0) + take
        need -= take
        disallowed += part
        lot = next((x for x in book.lots if x.fill_id == buy.id and x.kind == "long"), None)
        if lot is not None:
            lot.added_basis += part
        elif buy.filled_at > sale.filled_at:
            pending[buy.id] = pending.get(buy.id, 0.0) + part
        # a replacement bought before and already sold keeps nothing to adjust
    return disallowed
