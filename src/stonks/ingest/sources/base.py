"""Abstract base class for vendor data sources.

A DataSource is a thin HTTP client: it fetches raw rows for a single ticker
and maps them into canonical row schemas (:class:`RawPriceBar`,
:class:`FinancialStatementsBundle`, or a :class:`MetadataBundle` covering
everything else).

Normalization and persistence are the pipeline's job; sources don't touch the lake.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from datetime import date

from stonks.core.interval import Interval
from stonks.ingest.calendar_schemas import DividendEventRow, EarningsEventRow, EconomicEventRow
from stonks.ingest.metadata_bundle import MetadataBundle
from stonks.ingest.schemas import (
    DefiTvlRow,
    ExchangeInfo,
    FinancialStatementsBundle,
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


class UnsupportedCapabilityError(DataSourceError):
    """The source does not offer this kind of data at all (e.g. prices from
    a DeFi-TVL vendor). A :class:`DataSourceError`, so the pipeline counts
    the unit as failed and moves on instead of aborting the run."""


class DataSource(ABC):
    source_id: str  # unique id, e.g. "eodhd", "yahoo"

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
