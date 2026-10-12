"""Abstract base class for vendor data sources.

A DataSource is a thin HTTP client: it fetches raw rows for a single ticker
and maps them into canonical row schemas (:class:`RawPriceBar`,
:class:`FinancialStatementsBundle`, or a :class:`MetadataBundle` covering
everything else).

Normalization and persistence are the pipeline's job; sources don't touch the lake.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date
from typing import Protocol

from stonks.core.interval import Interval
from stonks.ingest.calendar_schemas import DividendEventRow, EarningsEventRow, EconomicEventRow
from stonks.ingest.filing_schemas import CorporateFilingRow, InstitutionalHoldingRow
from stonks.ingest.metadata_bundle import (
    METADATA_PARTS,
    CompanyData,
    CompanyPart,
    MetadataBundle,
)
from stonks.ingest.option_schemas import OptionQuoteRow
from stonks.ingest.reference_schemas import (
    ExchangeDetailsRow,
    IdentifierPage,
    IndexComponentsRow,
    IpoEventRow,
    SplitEventRow,
    TreasuryCurve,
    TreasuryRateRow,
)
from stonks.ingest.schemas import (
    BorrowRateRow,
    DefiTvlRow,
    ExchangeInfo,
    FinancialStatementsBundle,
    FundHoldingRow,
    FxRateRow,
    InsiderTransactionRow,
    IntradayBar,
    MacroIndicatorRow,
    RawPriceBar,
    SymbolListing,
)


class DataSourceError(RuntimeError):
    """Base for vendor-class errors callers can soft-fail on (free-tier
    blocks, vendor outages, malformed responses). The pipeline narrows its
    per-ticker ``except`` to this type plus ``requests.RequestException``
    / ``json.JSONDecodeError`` / ``pydantic.ValidationError`` so genuine
    programmer bugs (KeyError, AttributeError, TypeError…) surface loudly
    instead of being silently filed as "ticker had no data."""


class QuotaExceededError(DataSourceError):
    """The vendor refused a request because the account used its allowance
    for the day. Retrying before the vendor's reset only fails again."""


class UnknownSymbolError(DataSourceError):
    """The source does not know this ticker or filer at all. Asking again
    gives the same answer, so a backfill marks the unit done with nothing
    stored, like an HTTP 404."""


class RateLimitedError(DataSourceError):
    """The source refused requests for going too fast (after its own
    retries). Not the unit's fault: a backfill pauses and asks again later,
    with no attempt counted."""


class UnsupportedCapabilityError(DataSourceError):
    """The source does not offer this kind of data at all (e.g. prices from
    a DeFi-TVL vendor). A :class:`DataSourceError`, so the pipeline counts
    the unit as failed and moves on instead of aborting the run."""


@dataclass(frozen=True)
class ApiUsage:
    """What the vendor says the account used today, in its own units
    (EODHD counts API calls, some endpoints costing more than one)."""

    used_today: int
    daily_limit: int | None = None
    #: The day the vendor counts ``used_today`` for (``None``: not said).
    day: date | None = None


class RequestMeter(Protocol):
    """Told about every request a source is about to send, with what it
    costs in the vendor's units. It may sleep (to pace requests) or raise
    (to stop at a budget) before the request leaves."""

    def before_request(self, endpoint: str, cost: int) -> None: ...


class DataSource(ABC):
    source_id: str  # unique id, e.g. "eodhd", "yahoo"

    def set_request_meter(self, meter: RequestMeter | None) -> None:
        """Have ``meter`` told about every request this source sends.

        Optional capability: sources that do not count requests ignore it."""
        del meter

    def fetch_usage(self) -> ApiUsage | None:
        """How much of the daily allowance the account used today, from
        the vendor. ``None`` when the source cannot say."""
        return None

    @abstractmethod
    def list_tickers(self, exchange: str) -> list[str]: ...

    @abstractmethod
    def fetch_prices(
        self, ticker: str, since: date | None = None, until: date | None = None
    ) -> Iterable[RawPriceBar]: ...

    @abstractmethod
    def fetch_fundamentals(self, ticker: str) -> FinancialStatementsBundle:
        """Return all three financial statements for the ticker as one bundle.

        Vendors that expose the three statements in a single endpoint
        (e.g. EODHD's ``/fundamentals``) issue one call and split the
        result into the three streams. Sources that only carry one
        statement type leave the others empty.
        """

    # Extended fundamentals: ticker profile, dividends, insiders, news, analysts,
    # shares outstanding, employees, segmentation — all in one bundle. Sources
    # that only cover a subset return an empty bundle + populate only what
    # they have. Default implementation returns an empty bundle so existing
    # subclasses don't have to opt in.
    def fetch_metadata(self, ticker: str) -> MetadataBundle:
        return MetadataBundle()

    def fetch_company_data(self, ticker: str, parts: Collection[CompanyPart]) -> CompanyData:
        """Only the ``parts`` of one company's data a caller asks for.

        The default calls :meth:`fetch_fundamentals`,
        :meth:`fetch_fund_holdings` and :meth:`fetch_metadata` as needed
        and keeps the metadata parts asked for. Sources that can fetch
        the parts one by one, or share one response between them,
        override it."""
        wanted = set(parts)
        statements = self.fetch_fundamentals(ticker) if "statements" in wanted else None
        holdings = tuple(self.fetch_fund_holdings(ticker)) if "fund_holdings" in wanted else ()
        metadata = MetadataBundle()
        if wanted & METADATA_PARTS:
            metadata = _keep_parts(self.fetch_metadata(ticker), wanted)
        return CompanyData(metadata=metadata, statements=statements, fund_holdings=holdings)

    def fetch_intraday_bars(
        self,
        ticker: str,
        interval: Interval,
        since: date | None = None,
        until: date | None = None,
    ) -> Iterable[IntradayBar]:
        """Return sub-daily bars at the given native interval. Subclasses
        that don't support intraday data can leave the default empty
        implementation."""
        return ()

    def list_symbols(self, exchange: str) -> list[SymbolListing]:
        """Every symbol on ``exchange``, active and delisted, with what the
        source knows about it (security type, delisted flag, currency).

        Optional capability: the default wraps :meth:`list_tickers`, so
        every listing is marked active with no security type. Sources whose
        discovery endpoint says more override it."""
        return [SymbolListing(ticker=t) for t in self.list_tickers(exchange)]

    def fetch_bulk_eod(self, exchange: str, day: date) -> list[RawPriceBar]:
        """Daily bars of every symbol on ``exchange`` for one ``day`` in a
        single call. Refreshing a whole exchange this way costs one request
        a day instead of one per ticker.

        Optional capability: the default raises
        :class:`UnsupportedCapabilityError`, and callers fall back to
        :meth:`fetch_prices` per ticker (see ``stonks.ingest.ensure``)."""
        raise UnsupportedCapabilityError(
            f"{self.source_id} does not serve bulk daily bars ({exchange} {day})"
        )

    def list_exchanges(self) -> Iterable[ExchangeInfo]:
        """Return the exchanges this source can serve. Sources without a
        discovery endpoint inherit the empty default."""
        return ()

    def fetch_macro_indicator(
        self,
        country_iso: str,
        indicator: str,
    ) -> Iterable[MacroIndicatorRow]:
        """Return the time series for one macroeconomic indicator in one
        country. ``country_iso`` is ISO 3166-1 alpha-3.

        Sources without a macro endpoint inherit the empty default so
        only the adapters that actually serve macro data have to opt in.
        """
        del country_iso, indicator
        return ()

    def list_macro_indicators(self) -> Sequence[str]:
        """The macro indicator keys :meth:`fetch_macro_indicator` serves.
        Sources without macro data inherit the empty default."""
        return ()

    def fetch_option_quotes(
        self, underlying: str, since: date | None = None, until: date | None = None
    ) -> Iterable[OptionQuoteRow]:
        """End-of-day option quotes of every listed contract on
        ``underlying`` for the days in ``[since, until]`` (roadmap 17.1).
        ``since == until`` is one day's chain snapshot.

        Optional capability: the default raises
        :class:`UnsupportedCapabilityError`."""
        del since, until
        raise UnsupportedCapabilityError(
            f"{self.source_id} does not serve option quotes ({underlying!r})"
        )

    def fetch_chain_tvl(self, chain: str, since: date | None = None) -> Iterable[DefiTvlRow]:
        """Return the daily DeFi total-value-locked series for one chain
        (canonical lower-case name, e.g. ``"ethereum"``), optionally only
        observations on or after ``since``.

        Optional capability: sources without TVL data inherit this default,
        which raises :class:`UnsupportedCapabilityError` so a mis-pointed
        ``--source`` shows up as a failed unit instead of a silent empty run.
        """
        del since
        raise UnsupportedCapabilityError(f"{self.source_id} does not serve DeFi TVL ({chain!r})")

    def fetch_fx_rates(
        self,
        base: str,
        quote: str,
        since: date | None = None,
        until: date | None = None,
    ) -> Iterable[FxRateRow]:
        """Daily FX rates for one pair: units of ``quote`` per one ``base``
        (ISO 4217 codes), optionally only ``since <= day <= until``.

        Optional capability: the default raises
        :class:`UnsupportedCapabilityError`, so a source without forex
        data shows up as a failed pair instead of a silent empty run."""
        del since, until
        raise UnsupportedCapabilityError(
            f"{self.source_id} does not serve FX rates ({base}{quote})"
        )

    def fetch_borrow_rates(self, market: str) -> Iterable[BorrowRateRow]:
        """Today's stock borrow terms for every stock of one ``market`` (a
        source-defined name, e.g. ``usa``), roadmap 19.3.

        Optional capability: the default raises
        :class:`UnsupportedCapabilityError`, so a source without borrow
        data shows up as a failed market instead of a silent empty run."""
        raise UnsupportedCapabilityError(
            f"{self.source_id} does not serve borrow rates ({market!r})"
        )

    def fetch_fund_holdings(self, fund: str) -> Iterable[FundHoldingRow]:
        """The latest published holdings of one fund (an ETF), roadmap
        23.14. A stock or a fund the vendor lists no holdings for returns
        an empty list.

        Optional capability: the default raises
        :class:`UnsupportedCapabilityError`, so a source without fund data
        shows up as a failed fund instead of a silent empty run."""
        raise UnsupportedCapabilityError(
            f"{self.source_id} does not serve fund holdings ({fund!r})"
        )

    # ---- regulatory filings (roadmap 23.13) -----------------------------------
    # Optional capabilities: the defaults raise UnsupportedCapabilityError.

    def fetch_filings(
        self,
        ticker: str,
        since: date | None = None,
        until: date | None = None,
        forms: Sequence[str] | None = None,
    ) -> Iterable[CorporateFilingRow]:
        """Filings the company made with an acceptance day in ``[since,
        until]``, of ``forms`` (``None``: every form)."""
        del since, until, forms
        raise UnsupportedCapabilityError(f"{self.source_id} does not serve filings ({ticker!r})")

    def fetch_insider_filings(
        self, ticker: str, since: date | None = None, until: date | None = None
    ) -> Iterable[InsiderTransactionRow]:
        """Insider trades from ownership reports accepted in ``[since,
        until]``, each with the report's acceptance time as ``known_at``."""
        del since, until
        raise UnsupportedCapabilityError(
            f"{self.source_id} does not serve insider filings ({ticker!r})"
        )

    def fetch_institutional_holdings(
        self, filer_cik: str, since: date | None = None, until: date | None = None
    ) -> Iterable[InstitutionalHoldingRow]:
        """The lines of every quarterly holdings report a manager filed in
        ``[since, until]``."""
        del since, until
        raise UnsupportedCapabilityError(
            f"{self.source_id} does not serve institutional holdings ({filer_cik!r})"
        )

    # ---- event calendars (roadmap 20.7) ----------------------------------------
    # Optional capabilities: the defaults raise UnsupportedCapabilityError so a
    # source without calendars shows up as a failed unit, not an empty run.

    def fetch_earnings_calendar(
        self, start: date, end: date, tickers: Sequence[str] | None = None
    ) -> Iterable[EarningsEventRow]:
        """Earnings reports dated ``start`` to ``end``, for ``tickers`` or
        the whole market (``None``)."""
        del start, end, tickers
        raise UnsupportedCapabilityError(f"{self.source_id} does not serve an earnings calendar")

    def fetch_dividend_calendar(
        self, start: date, end: date, tickers: Sequence[str] | None = None
    ) -> Iterable[DividendEventRow]:
        """Ex-dividend dates ``start`` to ``end``, for ``tickers`` or the
        whole market (``None``)."""
        del start, end, tickers
        raise UnsupportedCapabilityError(f"{self.source_id} does not serve a dividend calendar")

    def fetch_economic_events(
        self, start: date, end: date, countries: Sequence[str] | None = None
    ) -> Iterable[EconomicEventRow]:
        """Scheduled macro releases ``start`` to ``end`` (UTC days), for
        ``countries`` (ISO alpha-2) or every country (``None``)."""
        del start, end, countries
        raise UnsupportedCapabilityError(f"{self.source_id} does not serve economic events")

    # ---- reference data (lake migration 026) -----------------------------------
    # Optional capabilities: the defaults raise UnsupportedCapabilityError.

    def fetch_index_components(self, index_code: str, as_of: date) -> IndexComponentsRow:
        """An index's members on ``as_of`` plus every dated change the
        source knows (``index_code`` is the source's own code)."""
        del as_of
        raise UnsupportedCapabilityError(
            f"{self.source_id} does not serve index members ({index_code!r})"
        )

    def fetch_exchange_details(self, exchange: str, start: date, end: date) -> ExchangeDetailsRow:
        """One exchange's trading hours, time zone, and the holidays from
        ``start`` to ``end``."""
        del start, end
        raise UnsupportedCapabilityError(
            f"{self.source_id} does not serve exchange details ({exchange!r})"
        )

    def fetch_treasury_rates(self, curve: TreasuryCurve, year: int) -> Iterable[TreasuryRateRow]:
        """Every US Treasury rate of ``curve`` in ``year``."""
        raise UnsupportedCapabilityError(
            f"{self.source_id} does not serve Treasury rates ({curve} {year})"
        )

    def fetch_ipo_calendar(self, start: date, end: date) -> Iterable[IpoEventRow]:
        """IPOs listing (or expected to list) ``start`` to ``end``."""
        del start, end
        raise UnsupportedCapabilityError(f"{self.source_id} does not serve an IPO calendar")

    def fetch_split_calendar(self, start: date, end: date) -> Iterable[SplitEventRow]:
        """Share splits dated ``start`` to ``end``, the whole market."""
        del start, end
        raise UnsupportedCapabilityError(f"{self.source_id} does not serve a split calendar")

    def fetch_identifiers(self, exchange: str, offset: int, limit: int) -> IdentifierPage:
        """One page of the domain identifiers (ISIN, CUSIP, CIK, FIGI, LEI)
        of an exchange's tickers."""
        del offset, limit
        raise UnsupportedCapabilityError(
            f"{self.source_id} does not serve identifiers ({exchange!r})"
        )

    def list_tvl_chains(self) -> Sequence[str]:
        """Every chain :meth:`fetch_chain_tvl` serves (canonical names).
        Sources without TVL data inherit the empty default."""
        return ()


#: Bundle fields each metadata part fills.
_PART_FIELDS: dict[str, tuple[str, ...]] = {
    "dividends": ("dividends",),
    "splits": ("splits",),
    "market_cap": ("market_cap_history",),
    "news": ("news",),
    "sentiment": ("news_sentiment",),
    "insiders": ("insider_transactions",),
}


def _keep_parts(bundle: MetadataBundle, wanted: set[CompanyPart]) -> MetadataBundle:
    """``bundle`` with the parts not in ``wanted`` cleared. Every field no
    other part names belongs to ``profile``."""
    cleared: dict[str, object] = {}
    for part, fields in _PART_FIELDS.items():
        if part not in wanted:
            cleared.update(dict.fromkeys(fields, ()))
    if "profile" not in wanted:
        owned = {f for fields in _PART_FIELDS.values() for f in fields}
        for name, value in vars(bundle).items():
            if name not in owned:
                cleared[name] = () if isinstance(value, tuple) else None
    return replace(bundle, **cleared)  # type: ignore[arg-type]
