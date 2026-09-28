"""Look-through exposure (roadmap 23.14): a held fund counts as the
companies it holds, by its published weights, so a book's real weight in a
sector, a country or one name shows (Apple through AAPL, SPY and QQQ).

The part of a fund its list does not explain shows as ``not listed``. Cash
is its own group, so the weights add up to the book's total value, like
:func:`stonks.insights.allocation`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from stonks.funds import FundSnapshot
from stonks.insights.allocation import CASH, UNKNOWN
from stonks.insights.book import Book
from stonks.insights.models import FundCoverage, LookThrough, LookThroughName, LookThroughSlice

NOT_LISTED = "not listed"
#: Names shown unless the caller asks for another number.
TOP_NAMES = 20


@dataclass
class _Acc:
    direct: float = 0.0
    fund: float = 0.0
    funds: set[str] = field(default_factory=set)
    name: str | None = None

    @property
    def value(self) -> float:
        return self.direct + self.fund


def look_through(
    book: Book,
    funds: Mapping[str, FundSnapshot],
    *,
    countries: Mapping[str, str | None],
    names: Mapping[str, str | None],
    top: int = TOP_NAMES,
) -> LookThrough:
    """Sector, country and single-name exposure of ``book`` with each held
    fund in ``funds`` split into its holdings. ``countries`` and ``names``
    label direct holdings by ticker."""
    sector: dict[str, _Acc] = {}
    country: dict[str, _Acc] = {}
    people: dict[str, _Acc] = {}
    coverage: list[FundCoverage] = []
    fund_value = 0.0
    listed = 0.0

    def add(groups: dict[str, _Acc], key: str, value: float, via: str | None) -> _Acc:
        acc = groups.setdefault(key, _Acc())
        if via is None:
            acc.direct += value
        else:
            acc.fund += value
            acc.funds.add(via)
        return acc

    for h in book.priced:
        value = h.market_value or 0.0
        snap = funds.get(h.ticker) if h.ticker else None
        if snap is None:
            add(sector, h.sector or UNKNOWN, value, None)
            add(country, (countries.get(h.ticker) if h.ticker else None) or UNKNOWN, value, None)
            acc = add(people, h.symbol, value, None)
            acc.name = acc.name or (names.get(h.ticker) if h.ticker else None)
            continue
        fund_value += value
        # A list whose rounded weights add up past 100 % is scaled back, so
        # the fund never counts for more than its own value.
        listed_weight = sum(c.weight for c in snap.constituents)
        scale = snap.covered / listed_weight if listed_weight > snap.covered else 1.0
        for c in snap.constituents:
            part = value * c.weight * scale
            add(sector, c.sector or UNKNOWN, part, snap.fund)
            add(country, c.country or UNKNOWN, part, snap.fund)
            acc = add(people, c.holding, part, snap.fund)
            acc.name = acc.name or c.name
        rest = value * (1.0 - snap.covered)
        listed += value * snap.covered
        if rest:
            add(sector, NOT_LISTED, rest, snap.fund)
            add(country, NOT_LISTED, rest, snap.fund)
        coverage.append(
            FundCoverage(
                fund=snap.fund,
                as_of=snap.as_of,
                source=snap.source,
                value=value,
                holdings=len(snap.constituents),
                covered=snap.covered,
            )
        )
    total = book.total_value
    if book.cash and (sector or country):
        add(sector, CASH, book.cash, None)
        add(country, CASH, book.cash, None)
    for key, acc in people.items():
        acc.name = acc.name or names.get(key)
    ranked = sorted(people.items(), key=lambda kv: (-abs(kv[1].value), kv[0]))[: max(top, 0)]
    return LookThrough(
        sector=_slices(sector, total),
        country=_slices(country, total),
        names=[
            LookThroughName(
                key=key,
                name=acc.name,
                value=acc.value,
                weight=_weight(acc.value, total),
                direct_value=acc.direct,
                fund_value=acc.fund,
                funds=sorted(acc.funds),
            )
            for key, acc in ranked
        ],
        funds=sorted(coverage, key=lambda f: f.fund),
        fund_value=fund_value,
        listed_fund_value=listed,
    )


def _weight(value: float, total: float) -> float | None:
    return value / total if total > 0 else None


def _slices(groups: Mapping[str, _Acc], total: float) -> list[LookThroughSlice]:
    rows = [
        LookThroughSlice(
            key=key,
            value=acc.value,
            weight=_weight(acc.value, total),
            direct_value=acc.direct,
            fund_value=acc.fund,
        )
        for key, acc in groups.items()
    ]
    return sorted(rows, key=lambda s: (-abs(s.value), s.key))
