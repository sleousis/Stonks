"""Vendor-neutral rows for reference data (lake migration 026): index
members and their history, exchange trading hours and holidays, US
Treasury rates, the IPO and split calendars, and instrument identifiers.

Every :class:`~stonks.ingest.sources.base.DataSource` that serves one of
these maps its vendor JSON into these rows at parse time, so the lake
tables never carry vendor field names or vendor vocabulary.
"""

from __future__ import annotations

from datetime import date, time
from typing import Literal

from pydantic import Field

from stonks.ingest.schemas import FrozenRow

#: Which Treasury series a rate belongs to. ``bill``: daily bill rates by
#: weeks to maturity. ``par_nominal`` and ``par_real``: the par yield curves.
#: ``long_term`` and ``long_term_real``: the long-term composite rates.
TreasuryCurve = Literal["bill", "par_nominal", "par_real", "long_term", "long_term_real"]

#: What a Treasury number measures. Bills quote a bank discount and a coupon
#: equivalent (and their averages); every other curve quotes a ``yield``.
TreasuryMeasure = Literal[
    "yield", "bank_discount", "coupon_equivalent", "bank_discount_avg", "coupon_equivalent_avg"
]

#: Where an IPO stands. Adapters map vendor strings into these.
IpoStatus = Literal["expected", "filed", "priced", "amended", "withdrawn", "other"]

#: What kind of day off a holiday is. ``official``: the exchange is shut.
#: ``bank``: a bank holiday the vendor lists (often shut too). ``other``.
HolidayKind = Literal["official", "bank", "other"]


class IndexChangeRow(FrozenRow):
    """A member joining (``add``) or leaving (``remove``) an index."""

    ticker: str = Field(min_length=1)
    change_date: date
    action: Literal["add", "remove"]


class IndexComponentsRow(FrozenRow):
    """An index's members on ``as_of`` plus every dated change the source
    knows. ``index_id`` is a canonical ``lower_snake_case`` code."""

    index_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_]{0,63}$")
    as_of: date
    name: str | None = None
    members: tuple[str, ...] = ()
    changes: tuple[IndexChangeRow, ...] = ()


class ExchangeHolidayRow(FrozenRow):
    """A day the exchange is shut or closes early (``early_close`` set,
    local time)."""

    holiday_date: date
    name: str | None = None
    kind: HolidayKind = "other"
    early_close: time | None = None


class ExchangeDetailsRow(FrozenRow):
    """One exchange's trading hours, time zone and holidays. Times are
    ``HH:MM:SS``: ``open_local``/``close_local`` in ``timezone``,
    ``open_utc``/``close_utc`` in UTC. ``working_days`` lists ISO weekdays
    (1 = Monday)."""

    exchange: str = Field(min_length=1)
    name: str | None = None
    operating_mics: tuple[str, ...] = ()
    country: str | None = None
    currency: str | None = None
    timezone: str | None = None
    open_local: time | None = None
    close_local: time | None = None
    open_utc: time | None = None
    close_utc: time | None = None
    working_days: tuple[int, ...] = ()
    holidays: tuple[ExchangeHolidayRow, ...] = ()


class TreasuryRateRow(FrozenRow):
    """One US Treasury rate on one day, in percent. ``tenor`` is a
    normalized code: ``4W``, ``1M``, ``1.5M``, ``10Y``, ``20Y``, ``10Y+``."""

    curve: TreasuryCurve
    tenor: str = Field(min_length=1, max_length=8)
    measure: TreasuryMeasure = "yield"
    observation_date: date
    rate: float | None = None
    maturity_date: date | None = None
    cusip: str | None = None


class IpoEventRow(FrozenRow):
    """One initial public offering. Prices are per share in ``currency``,
    ``None`` when the source has none yet."""

    ticker: str = Field(min_length=1)
    name: str | None = None
    currency: str | None = None
    filing_date: date | None = None
    amended_date: date | None = None
    listing_date: date | None = None
    price_low: float | None = None
    price_high: float | None = None
    offer_price: float | None = None
    shares: float | None = None
    status: IpoStatus = "other"


class SplitEventRow(FrozenRow):
    """One announced or past share split: ``old_shares`` become
    ``new_shares`` on ``split_date``."""

    ticker: str = Field(min_length=1)
    split_date: date
    old_shares: float | None = Field(default=None, gt=0)
    new_shares: float | None = Field(default=None, gt=0)
    optionable: bool | None = None


class InstrumentIdentifiersRow(FrozenRow):
    """The domain identifiers of one ticker. CIK is ten digits."""

    ticker: str = Field(min_length=1)
    isin: str | None = None
    cusip: str | None = None
    cik: str | None = None
    open_figi: str | None = None
    lei: str | None = None


class IdentifierPage(FrozenRow):
    """One page of identifiers and the total the source can page through."""

    rows: tuple[InstrumentIdentifiersRow, ...] = ()
    #: Rows the page held, those with no identifier too (for paging).
    size: int = 0
    total: int = 0
