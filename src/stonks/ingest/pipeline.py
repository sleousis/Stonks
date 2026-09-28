"""Orchestrates fetch → normalize → idempotent upsert for a single DataSource.

Per-ticker failures are soft: logged, counted, and reflected in the
``ingest_runs`` row; the pipeline continues through the remaining tickers.
Final ``status`` is ``"ok"`` when all succeed, ``"partial"`` when at least one
succeeded, ``"error"`` when all failed, and always ``"ok"`` for an empty batch
(so callers can safely drive idempotent cron jobs).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from functools import partial
from typing import Any, Literal, cast

import pandas as pd
import pydantic
import requests

from stonks.core.interval import Interval
from stonks.ingest.adjustment import adj_ratio, adjustment_drift
from stonks.ingest.metadata_bundle import MetadataBundle
from stonks.ingest.quality import (
    BarQualityChecker,
    RunQuality,
    SeriesWarning,
    history_before,
    quarantine_bars,
    record_run_quality,
    splits_frame,
)
from stonks.ingest.redact import format_exception
from stonks.ingest.schemas import (
    DefiTvlRow,
    FinancialStatementsBundle,
    IntradayBar,
    MacroIndicatorRow,
    RawPriceBar,
)
from stonks.ingest.sessions import SessionCloses, default_sessions, drop_open_sessions
from stonks.ingest.sources.base import DataSource, DataSourceError
from stonks.logging import get_logger
from stonks.notify.base import Notification, Notifier
from stonks.store.lake import DuckDBLake

# Narrow per-ticker soft-fail surface (I1): vendor-class errors and
# parse/transport-class errors are expected and soft-fail; programmer bugs
# (KeyError, AttributeError, TypeError, NameError…) propagate so tests
# catch them instead of having them masked as "this ticker had no data."
_SOFT_FAIL_EXCEPTIONS = (
    DataSourceError,
    requests.RequestException,
    json.JSONDecodeError,
    pydantic.ValidationError,
)

DateRanges = Mapping[str, Sequence[tuple[date, date]]]

_PRICE_COLUMNS = ("open", "high", "low", "close")

#: The calendars :meth:`IngestPipeline.run_calendars` can pull.
CalendarKind = Literal["earnings", "dividends", "economic"]
CALENDAR_KINDS: tuple[CalendarKind, ...] = ("earnings", "dividends", "economic")


@dataclass(frozen=True)
class IngestRunResult:
    run_id: int
    kind: str
    status: str  # "ok" | "partial" | "error"
    tickers_ok: int
    tickers_failed: int
    # Bar runs only: the quality summary stored on ``ingest_runs.quality_json``.
    quality: dict[str, Any] | None = None
    #: The units that failed, by ticker (or their log context), in run order.
    failed: tuple[str, ...] = ()


class IngestPipeline:
    """Every ``run_*`` method funnels through :meth:`_run_units`, which owns
    the ``ingest_runs`` row lifecycle: open, per-unit soft-fail accounting,
    and a guaranteed close — even when a non-soft-fail exception (a lake
    error, a programmer bug, Ctrl-C) escapes the loop, in which case the row
    is closed as ``error`` and the exception re-raised. Operators never have
    to triage orphaned ``running`` rows by hand. The overall run is **not**
    atomic: units that succeeded before the failure stay committed.

    Bar runs (:meth:`run_prices`, :meth:`run_intraday_bars`) also:

    - validate each ticker's batch with ``quality`` (a default
      :class:`BarQualityChecker` when omitted; disable it through its
      config) and write rejected rows to ``quarantined_bars`` instead of
      the bar store;
    - drop daily bars whose session has not closed at ``clock()`` (a
      vendor's "today" bar during the session is not final; the next
      ingest after the close stores it);
    - retry a ticker whose primary fetch soft-fails on ``fallback`` (when
      given), recording the supplier in the run's quality summary; the
      ``ingest_runs`` row keeps the primary's id as its ``source``. With
      ``ranges`` the fallback is asked only for that ticker's ranges;
    - store the run's quality summary on ``ingest_runs.quality_json`` and
      send one warning through ``notifier`` when it breaches the
      checker's alert thresholds;
    - for daily bars, compare the batch's ``adj_close / close`` with the
      stored bars it overlaps. When the vendor restated it (a split or
      dividend since the last fetch, beyond ``adjustment_tolerance``), the
      stored bars older than the batch are scaled onto the new basis, so
      the series never mixes two bases (BE-09). ``None`` turns it off.
    """

    def __init__(
        self,
        source: DataSource,
        lake: DuckDBLake,
        *,
        quality: BarQualityChecker | None = None,
        fallback: DataSource | None = None,
        notifier: Notifier | None = None,
        clock: Callable[[], datetime] | None = None,
        sessions: SessionCloses | None = None,
        adjustment_tolerance: float | None = 5e-4,
    ):
        self._source = source
        self._adjustment_tolerance = adjustment_tolerance
        self._lake = lake
        self._quality = quality if quality is not None else BarQualityChecker()
        self._fallback = fallback
        self._notifier = notifier
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sessions = sessions if sessions is not None else default_sessions()
        self._log = get_logger("stonks.ingest.pipeline").bind(source=source.source_id)

    def run_prices(
        self,
        tickers: Sequence[str],
        since: date | None = None,
        until: date | None = None,
        *,
        ranges: DateRanges | None = None,
    ) -> IngestRunResult:
        """Daily bars of ``tickers`` over ``[since, until]``. ``ranges``
        (ticker to date ranges) narrows what a fallback source is asked
        for when the primary fails."""

        def fetch(src: DataSource, t: str, s: date | None, u: date | None) -> Iterable[Any]:
            return src.fetch_prices(t, since=s, until=u)

        return self._run_bars(
            kind="prices",
            tickers=tickers,
            interval=Interval.DAY_1,
            fetch=lambda src, t: fetch(src, t, since, until),
            fallback_fetch=_ranged(fetch, ranges, since, until),
            to_df=_prices_to_df,
            upsert=self._lake.upsert_prices,
            as_of=until,
        )

    def run_intraday_bars(
        self,
        tickers: Sequence[str],
        interval: Interval,
        since: date | None = None,
        until: date | None = None,
        *,
        ranges: DateRanges | None = None,
    ) -> IngestRunResult:
        def fetch(src: DataSource, t: str, s: date | None, u: date | None) -> Iterable[Any]:
            return src.fetch_intraday_bars(t, interval, s, u)

        return self._run_bars(
            kind=f"intraday:{interval.code}",
            tickers=tickers,
            interval=interval,
            fetch=lambda src, t: fetch(src, t, since, until),
            fallback_fetch=_ranged(fetch, ranges, since, until),
            to_df=_intraday_to_df,
            upsert=lambda df: self._lake.upsert_bars(df, interval=interval),
            as_of=until,
        )

    def run_fundamentals(self, tickers: Sequence[str]) -> IngestRunResult:
        """Pull each ticker's :class:`FinancialStatementsBundle` and upsert
        the three statements into their dedicated tables.

        One vendor call per ticker (the bundle), three lake writes
        wrapped in a per-ticker transaction so a partial failure on one
        ticker never leaves that ticker half-populated.
        """

        def ingest(ticker: str) -> dict[str, Any]:
            bundle = self._source.fetch_fundamentals(ticker)
            self._upsert_statements_bundle(bundle)
            return {
                "income_rows": len(bundle.income),
                "balance_rows": len(bundle.balance),
                "cashflow_rows": len(bundle.cashflow),
            }

        return self._run_units(
            kind="fundamentals",
            units=[({"ticker": t}, partial(ingest, t)) for t in tickers],
        )

    def _upsert_statements_bundle(self, bundle: FinancialStatementsBundle) -> None:
        # Atomic *per ticker* (not per run): a downstream NOT NULL
        # violation on one of the three tables rolls back the other two
        # so the ticker either has all three statements applied or
        # none at all. Other tickers in the same run are unaffected.
        with self._lake.transaction():
            self._lake.upsert_income_statement(_rows_to_df(bundle.income))
            self._lake.upsert_balance_sheet(_rows_to_df(bundle.balance))
            self._lake.upsert_cash_flow_statement(_rows_to_df(bundle.cashflow))

    def run_macro_indicators(
        self,
        countries: Sequence[str],
        indicators: Sequence[str],
    ) -> IngestRunResult:
        """Pull each ``(country, indicator)`` macro time series and upsert.

        Iterates the cross product of ``countries × indicators`` (the
        natural shape of the EODHD endpoint, which serves one country +
        one indicator per call). Each pair is one unit of work for the
        purposes of soft-fail accounting: a vendor outage on
        ``(USA, gdp_growth_annual)`` doesn't poison
        ``(DEU, real_gdp_total)``.

        ``tickers_ok`` / ``tickers_failed`` count *pairs*, not countries —
        we keep the existing ``IngestRunResult`` field names to avoid
        forking the result type for a single new flow.
        """

        def ingest(country: str, indicator: str) -> dict[str, Any]:
            rows = list(self._source.fetch_macro_indicator(country, indicator))
            self._lake.upsert_macro_indicators(_macro_to_df(rows))
            return {"rows": len(rows)}

        return self._run_units(
            kind="macro",
            event="macro",
            units=[
                ({"country_iso": c, "indicator": i}, partial(ingest, c, i))
                for c in countries
                for i in indicators
            ],
        )

    def run_defi_tvl(
        self,
        chains: Sequence[str],
        since: date | None = None,
    ) -> IngestRunResult:
        """Pull each chain's daily DeFi TVL series and upsert it into
        ``defi_tvl``. One chain is one unit of soft-fail accounting
        (``tickers_ok`` / ``tickers_failed`` count chains), so an unknown
        chain or a vendor outage on one never blocks the others."""

        def ingest(chain: str) -> dict[str, Any]:
            rows = list(self._source.fetch_chain_tvl(chain, since=since))
            self._lake.upsert_defi_tvl(_defi_tvl_to_df(rows))
            return {"rows": len(rows)}

        return self._run_units(
            kind="defi_tvl",
            event="chain",
            units=[({"chain": c}, partial(ingest, c)) for c in chains],
        )

    def run_fx_rates(
        self,
        pairs: Sequence[tuple[str, str]],
        since: date | None = None,
        until: date | None = None,
    ) -> IngestRunResult:
        """Pull daily FX rates per ``(base, quote)`` pair into ``fx_rates``
        (roadmap 20.5). One pair is one unit of soft-fail accounting."""

        def ingest(base: str, quote: str) -> dict[str, Any]:
            rows = list(self._source.fetch_fx_rates(base, quote, since=since, until=until))
            self._lake.upsert_fx_rates(_rows_to_df(rows))
            return {"rows": len(rows)}

        return self._run_units(
            kind="fx",
            event="pair",
            units=[({"pair": f"{b}{q}"}, partial(ingest, b, q)) for b, q in pairs],
        )

    def run_borrow_rates(self, markets: Sequence[str]) -> IngestRunResult:
        """Pull today's stock borrow rates of each market into
        ``borrow_rates`` (roadmap 19.3). One market is one unit of
        soft-fail accounting."""

        def ingest(market: str) -> dict[str, Any]:
            rows = list(self._source.fetch_borrow_rates(market))
            self._lake.upsert_borrow_rates(_rows_to_df(rows))
            return {"rows": len(rows)}

        return self._run_units(
            kind="borrow",
            event="market",
            units=[({"ticker": m, "market": m}, partial(ingest, m)) for m in markets],
        )

    def run_fund_holdings(self, funds: Sequence[str]) -> IngestRunResult:
        """Pull each fund's latest holdings into ``fund_holdings`` (roadmap
        23.14). One fund is one unit of soft-fail accounting."""

        def ingest(fund: str) -> dict[str, Any]:
            rows = list(self._source.fetch_fund_holdings(fund))
            self._lake.upsert_fund_holdings(_rows_to_df(rows))
            return {"rows": len(rows)}

        return self._run_units(
            kind="funds",
            event="fund",
            units=[({"ticker": f}, partial(ingest, f)) for f in funds],
        )

    def run_calendars(
        self,
        start: date,
        end: date,
        *,
        tickers: Sequence[str] | None = None,
        countries: Sequence[str] | None = None,
        kinds: Sequence[CalendarKind] = CALENDAR_KINDS,
    ) -> IngestRunResult:
        """Pull the event calendars dated ``start`` to ``end`` into the lake
        (roadmap 20.7): earnings and dividends for ``tickers`` (``None``:
        the whole market) and economic events for ``countries`` (``None``:
        every country). Each calendar is one unit of soft-fail accounting,
        so a vendor outage on one never blocks the others; a source without
        a calendar counts that unit as failed."""
        from stonks.calendars.store import CalendarStore

        store = CalendarStore(self._lake)
        sid = self._source.source_id
        picked = list(tickers) if tickers is not None else None

        def earnings() -> dict[str, Any]:
            rows = list(self._source.fetch_earnings_calendar(start, end, picked))
            return {"rows": store.upsert_earnings(rows, source=sid)}

        def dividends() -> dict[str, Any]:
            rows = list(self._source.fetch_dividend_calendar(start, end, picked))
            return {"rows": store.upsert_dividends(rows, source=sid)}

        def economic() -> dict[str, Any]:
            chosen = list(countries) if countries is not None else None
            rows = list(self._source.fetch_economic_events(start, end, chosen))
            return {"rows": store.upsert_economic(rows, source=sid)}

        work = {"earnings": earnings, "dividends": dividends, "economic": economic}
        return self._run_units(
            kind="calendars",
            event="calendar",
            units=[({"ticker": k, "calendar": k}, work[k]) for k in kinds],
        )

    # ---- regulatory filings (roadmap 23.13) ---------------------------------------

    def run_filings(
        self,
        tickers: Sequence[str],
        since: date | None = None,
        until: date | None = None,
        forms: Sequence[str] | None = None,
    ) -> IngestRunResult:
        """Pull each ticker's filings (acceptance time, form, current report
        items) into ``corporate_filings``. One ticker is one unit."""
        sid = self._source.source_id

        def ingest(ticker: str) -> dict[str, Any]:
            rows = list(self._source.fetch_filings(ticker, since=since, until=until, forms=forms))
            frame = _rows_to_df(rows)
            if not frame.empty:
                frame["source"] = sid
            return {"rows": self._lake.upsert_corporate_filings(frame)}

        return self._run_units(
            kind="filings", units=[({"ticker": t}, partial(ingest, t)) for t in tickers]
        )

    def run_insider_filings(
        self, tickers: Sequence[str], since: date | None = None, until: date | None = None
    ) -> IngestRunResult:
        """Pull each ticker's insider trades from ownership reports, with the
        report's acceptance time as ``known_at``, into
        ``insider_transactions``. One ticker is one unit."""

        def ingest(ticker: str) -> dict[str, Any]:
            rows = list(self._source.fetch_insider_filings(ticker, since=since, until=until))
            return {"rows": self._lake.upsert_insider_transactions(_rows_to_df(rows))}

        return self._run_units(
            kind="insider_filings", units=[({"ticker": t}, partial(ingest, t)) for t in tickers]
        )

    def run_institutional_holdings(
        self, filers: Sequence[str], since: date | None = None, until: date | None = None
    ) -> IngestRunResult:
        """Pull each manager's quarterly holdings reports into
        ``institutional_holdings``, naming each CUSIP's ticker when the
        lake knows it. One filer (a CIK) is one unit."""

        def ingest(filer: str) -> dict[str, Any]:
            rows = list(self._source.fetch_institutional_holdings(filer, since=since, until=until))
            known = self._lake.tickers_by_cusip([r.cusip for r in rows if r.ticker is None])
            rows = [
                r.model_copy(update={"ticker": known[r.cusip]})
                if r.ticker is None and r.cusip in known
                else r
                for r in rows
            ]
            frame = _rows_to_df(rows)
            if not frame.empty:
                frame["source"] = self._source.source_id
            return {"rows": self._lake.upsert_institutional_holdings(frame)}

        return self._run_units(
            kind="institutional_holdings",
            event="filer",
            units=[({"ticker": f, "filer": f}, partial(ingest, f)) for f in filers],
        )

    def run_metadata(self, tickers: Sequence[str]) -> IngestRunResult:
        """Pull the extended fundamentals bundle per ticker (profile, dividends,
        insiders, news + sentiment, analyst estimates + ratings, shares
        outstanding, employee count, segmentations) and upsert each non-empty
        part into its matching lake table."""

        def ingest(ticker: str) -> dict[str, Any]:
            bundle = self._source.fetch_metadata(ticker)
            self._upsert_bundle(bundle)
            return {"kinds_found": _non_empty_parts(bundle)}

        return self._run_units(
            kind="metadata",
            units=[({"ticker": t}, partial(ingest, t)) for t in tickers],
        )

    def _upsert_bundle(self, bundle: MetadataBundle) -> None:
        # Atomic per ticker (C2): if any single upsert raises, every prior
        # write in this bundle is rolled back. Without this wrapper a mid-
        # bundle failure would leave half the tables populated and half
        # not, then the outer per-ticker except in ``run_metadata`` would
        # mark the ticker "failed" while the lake stayed in an inconsistent
        # half-state.
        with self._lake.transaction():
            if bundle.profile is not None:
                self._lake.upsert_instrument_profile(_rows_to_df([bundle.profile]))
            if bundle.ticker_snapshot is not None:
                self._lake.upsert_ticker_snapshots(_rows_to_df([bundle.ticker_snapshot]))
            self._lake.upsert_dividends(_rows_to_df(bundle.dividends))
            self._lake.upsert_stock_splits(_rows_to_df(bundle.splits))
            self._lake.upsert_insider_transactions(_rows_to_df(bundle.insider_transactions))
            self._lake.upsert_news(_rows_to_df(bundle.news))
            self._lake.upsert_news_sentiment(_rows_to_df(bundle.news_sentiment))
            self._lake.upsert_earnings_announcements(_rows_to_df(bundle.earnings_announcements))
            self._lake.upsert_analyst_forecasts(_rows_to_df(bundle.analyst_forecasts))
            self._lake.upsert_analyst_ratings(_rows_to_df(bundle.analyst_ratings))
            self._lake.upsert_institutional_holders(_rows_to_df(bundle.institutional_holders))
            if bundle.esg_snapshot is not None:
                self._lake.upsert_esg_snapshots(_rows_to_df([bundle.esg_snapshot]))
            self._lake.upsert_esg_activities(_rows_to_df(bundle.esg_activities))
            self._lake.upsert_cross_listings(_rows_to_df(bundle.cross_listings))
            self._lake.upsert_officers(_rows_to_df(bundle.officers))
            self._lake.upsert_shares_outstanding(_rows_to_df(bundle.shares_outstanding))
            self._lake.upsert_employee_count(_rows_to_df(bundle.employee_count))
            self._lake.upsert_segmentation(_rows_to_df(bundle.segmentation))
            self._lake.upsert_market_cap_history(_rows_to_df(bundle.market_cap_history))
            # Per-asset-class profile rows (only the relevant one is set
            # per ticker by the source adapter).
            if bundle.crypto_profile is not None:
                self._lake.upsert_crypto_profile(_rows_to_df([bundle.crypto_profile]))
            if bundle.bond_profile is not None:
                self._lake.upsert_bond_profile(_rows_to_df([bundle.bond_profile]))
            if bundle.commodity_contract is not None:
                self._lake.upsert_commodity_contract(_rows_to_df([bundle.commodity_contract]))
            self._lake.upsert_bond_yields(_rows_to_df(bundle.bond_yields))

    def _run_bars(
        self,
        *,
        kind: str,
        tickers: Sequence[str],
        interval: Interval,
        fetch: Callable[[DataSource, str], Iterable[Any]],
        to_df: Callable[[Iterable[Any]], pd.DataFrame],
        upsert: Callable[[pd.DataFrame], Any],
        as_of: date | None,
        fallback_fetch: Callable[[DataSource, str], Iterable[Any]] | None = None,
    ) -> IngestRunResult:
        """Bar flavour of :meth:`_run_units`: fetch (with fallback) →
        DataFrame → validate → upsert the clean rows, quarantine the rest."""
        quality = RunQuality()
        run: dict[str, int] = {}
        stale_ref = as_of or datetime.now(UTC).date()

        def ingest(ticker: str) -> dict[str, Any]:
            rows, supplier = self._fetch_with_fallback(fetch, ticker, fallback_fetch)
            if supplier != self._source.source_id:
                quality.supplied_by[ticker] = supplier
            if not rows:
                quality.warnings.append(
                    SeriesWarning(ticker, "no_data", 0, "the source returned no bars")
                )
            frame = to_df(rows)
            if not frame.empty:
                # a source may answer with its own symbol; the series is the
                # ticker that was asked for
                frame["ticker"] = ticker
            if interval == Interval.DAY_1:
                cls = self._lake.get_asset_classes([ticker]).get(ticker)
                frame = drop_open_sessions(frame, self._sessions, ticker, self._clock(), cls)
            clean, quarantined = self._validate(
                frame, ticker, interval, stale_ref, run["id"], supplier, quality
            )
            if interval == Interval.DAY_1:
                self._readjust(ticker, clean, quality)
            upsert(clean)
            return {"rows": len(rows), "quarantined": quarantined, "supplied_by": supplier}

        try:
            result = self._run_units(
                kind=kind,
                units=[({"ticker": t}, partial(ingest, t)) for t in tickers],
                on_open=lambda run_id: run.__setitem__("id", run_id),
            )
        except BaseException:
            if "id" in run:
                record_run_quality(self._lake, run["id"], quality)
            raise
        record_run_quality(self._lake, result.run_id, quality)
        summary = quality.to_dict()
        self._alert(result, summary, quality.breaches(self._quality.config))
        return replace(result, quality=summary)

    def _readjust(self, ticker: str, clean: pd.DataFrame, quality: RunQuality) -> None:
        """Scale the stored daily bars older than ``clean`` onto the
        vendor's new adjustment basis when the overlap shows it moved."""
        if self._adjustment_tolerance is None or clean.empty:
            return
        days = pd.to_datetime(clean["date"])
        first, last = days.min(), days.max()
        stored = self._lake.get_bars(
            ticker, Interval.DAY_1, first.to_pydatetime(), last.to_pydatetime()
        )
        if stored.empty:
            return
        factor = adjustment_drift(
            _ratios(pd.to_datetime(stored["timestamp"]), stored),
            _ratios(days, clean),
            self._adjustment_tolerance,
        )
        if factor is None:
            return
        older = self._lake.get_bars(
            ticker,
            Interval.DAY_1,
            datetime(1900, 1, 1),
            (first - pd.Timedelta(microseconds=1)).to_pydatetime(),
        )
        quality.readjusted[ticker] = factor
        if older.empty:
            return
        older["adj_close"] = older["adj_close"] * factor
        self._lake.upsert_bars(older, interval=Interval.DAY_1)
        self._log.warning(
            "bars.readjusted",
            ticker=ticker,
            before=str(first.date()),
            bars=len(older),
            factor=factor,
        )

    def _fetch_with_fallback(
        self,
        fetch: Callable[[DataSource, str], Iterable[Any]],
        ticker: str,
        fallback_fetch: Callable[[DataSource, str], Iterable[Any]] | None = None,
    ) -> tuple[list[Any], str]:
        """Rows for ``ticker`` and the id of the source that supplied them.
        A soft-fail on the primary is retried once on the fallback; if both
        fail, one :class:`DataSourceError` carries both errors."""
        try:
            return list(fetch(self._source, ticker)), self._source.source_id
        except _SOFT_FAIL_EXCEPTIONS as exc:
            if self._fallback is None:
                raise
            primary_error = format_exception(exc)
        fallback_id = self._fallback.source_id
        self._log.warning(
            "ticker.fallback", ticker=ticker, fallback=fallback_id, error=primary_error
        )
        try:
            return list((fallback_fetch or fetch)(self._fallback, ticker)), fallback_id
        except _SOFT_FAIL_EXCEPTIONS as exc:
            raise DataSourceError(
                f"{self._source.source_id}: {primary_error}; "
                f"fallback {fallback_id}: {format_exception(exc)}"
            ) from exc

    def _validate(
        self,
        frame: pd.DataFrame,
        ticker: str,
        interval: Interval,
        as_of: date,
        run_id: int,
        supplier: str,
        quality: RunQuality,
    ) -> tuple[pd.DataFrame, int]:
        """Split ``frame`` into the rows to store and the number sent to
        quarantine. Daily frames carry ``date``; the checker and the
        quarantine table work on ``timestamp``."""
        if frame.empty:
            return frame, 0
        if not self._quality.config.enabled:
            quality.bars_checked += len(frame)
            # a row with a missing price never overwrites a stored bar,
            # even with the checker off (BE-37)
            missing = cast(pd.Series, frame[list(_PRICE_COLUMNS)].isna().any(axis=1))
            dropped = int(missing.sum())
            if dropped:
                self._log.warning("bars.missing_price_dropped", ticker=ticker, rows=dropped)
            return cast(pd.DataFrame, frame.loc[~missing]), 0
        if "timestamp" in frame.columns:
            timed = frame
        else:
            timed = frame.assign(timestamp=pd.to_datetime(frame["date"]))
        first = pd.Timestamp(timed["timestamp"].min()).to_pydatetime()
        history = history_before(
            self._lake, ticker, interval, first, self._quality.config.history_bars
        )
        batch = self._quality.check(
            timed,
            interval=interval,
            history=history,
            splits=splits_frame(self._lake, [ticker]),
            as_of=as_of,
            asset_class=self._lake.get_asset_classes([ticker]).get(ticker),
        )
        quality.add(len(frame), batch)
        rejected = batch.rejected_mask.to_numpy(dtype=bool)
        if rejected.any():
            bad = timed[rejected].assign(reasons=batch.reasons[rejected])
            quarantine_bars(self._lake, bad, run_id=run_id, interval=interval, source=supplier)
        spiked = rejected & batch.reasons.str.contains("price_spike").to_numpy(dtype=bool)
        self._drop_stored_spikes(
            ticker,
            interval,
            history,
            [ts for t, ts in batch.stored_spikes if t == ticker],
            timed[spiked],
            run_id,
            supplier,
            quality,
        )
        return frame[~rejected], int(rejected.sum())

    def _drop_stored_spikes(
        self,
        ticker: str,
        interval: Interval,
        history: pd.DataFrame,
        stored: list[pd.Timestamp],
        batch_spikes: pd.DataFrame,
        run_id: int,
        supplier: str,
        quality: RunQuality,
    ) -> None:
        """Remove spikes that already sit in the bar store: stored bars the
        batch showed to be spikes (moved to quarantine here), and stored
        copies of batch rows rejected as spikes (already quarantined) when
        the stored close is the same bad value."""
        if stored and not history.empty:
            hist = history.assign(timestamp=pd.to_datetime(history["timestamp"]))
            rows = hist[hist["timestamp"].isin(stored)].assign(reasons="price_spike")
            if not rows.empty:
                quarantine_bars(self._lake, rows, run_id=run_id, interval=interval, source=supplier)
                self._lake.delete_bars(ticker, interval, list(rows["timestamp"]))
                quality.bars_quarantined += len(rows)
                quality.reasons.update(["price_spike"] * len(rows))
                self._log.warning(
                    "bars.stored_spike_removed",
                    ticker=ticker,
                    timestamps=[str(t) for t in rows["timestamp"]],
                )
        if batch_spikes.empty:
            return
        stamps = pd.to_datetime(batch_spikes["timestamp"])
        on_disk = self._lake.get_bars(
            ticker, interval, stamps.min().to_pydatetime(), stamps.max().to_pydatetime()
        )
        if on_disk.empty:
            return
        bad_close = dict(zip(stamps, batch_spikes["close"].astype(float), strict=True))
        on_disk = on_disk.assign(timestamp=pd.to_datetime(on_disk["timestamp"]))
        same = [
            ts
            for ts, close in zip(on_disk["timestamp"], on_disk["close"], strict=True)
            if ts in bad_close and abs(float(close) - bad_close[ts]) <= 1e-9 * abs(bad_close[ts])
        ]
        if same:
            self._lake.delete_bars(ticker, interval, same)

    def _alert(self, result: IngestRunResult, summary: dict[str, Any], breaches: list[str]) -> None:
        if not breaches or self._notifier is None:
            return
        self._notifier.notify(
            Notification(
                level="warning",
                title=f"Ingest data quality: {result.kind}",
                message="; ".join(breaches),
                fields={
                    "run_id": result.run_id,
                    "source": self._source.source_id,
                    "kind": result.kind,
                    "bars_checked": summary["bars_checked"],
                    "bars_quarantined": summary["bars_quarantined"],
                    "reasons": summary["reasons"],
                    "warnings": summary["warnings"],
                    "warned_tickers": summary["warned_tickers"],
                    "supplied_by": summary["supplied_by"],
                },
            )
        )

    def _run_units(
        self,
        *,
        kind: str,
        units: Iterable[tuple[dict[str, Any], Callable[[], dict[str, Any]]]],
        event: str = "ticker",
        on_open: Callable[[int], None] | None = None,
    ) -> IngestRunResult:
        """Run each ``(log_context, work)`` unit under one ``ingest_runs`` row.

        ``work()`` fetches + upserts one unit and returns extra fields for
        the success log line. Soft-fail exceptions count the unit as failed
        and move on; anything else closes the row as ``error`` and re-raises.
        Error text is credential-scrubbed before it is logged or stored.
        """
        run_id = self._lake.open_ingest_run(source=self._source.source_id, kind=kind)
        if on_open is not None:
            on_open(run_id)
        log = self._log.bind(run_id=run_id, kind=kind)

        ok = 0
        failed = 0
        failed_units: list[str] = []
        last_error: str | None = None
        try:
            for context, work in units:
                try:
                    fields = work()
                except _SOFT_FAIL_EXCEPTIONS as exc:
                    failed += 1
                    failed_units.append(str(context.get("ticker", context)))
                    last_error = format_exception(exc)
                    log.warning(f"{event}.failed", **context, error=last_error)
                    continue
                ok += 1
                log.info(f"{event}.ingested", **context, **fields)
            status = _status(ok, failed)
        except BaseException as exc:
            # BaseException on purpose: SystemExit / KeyboardInterrupt must
            # also close the row to a terminal status before propagating.
            last_error = format_exception(exc)
            self._lake.close_ingest_run(
                run_id,
                tickers_ok=ok,
                tickers_failed=failed,
                status="error",
                error=last_error,
            )
            log.error("run.aborted", status="error", error=last_error)
            raise

        self._lake.close_ingest_run(
            run_id,
            tickers_ok=ok,
            tickers_failed=failed,
            status=status,
            error=last_error if status in ("error", "partial") else None,
        )
        log.info("run.finished", status=status, tickers_ok=ok, tickers_failed=failed)
        return IngestRunResult(
            run_id=run_id,
            kind=kind,
            status=status,
            tickers_ok=ok,
            tickers_failed=failed,
            failed=tuple(failed_units),
        )


def _ranged(
    fetch: Callable[[DataSource, str, date | None, date | None], Iterable[Any]],
    ranges: DateRanges | None,
    since: date | None,
    until: date | None,
) -> Callable[[DataSource, str], Iterable[Any]]:
    """A per-ticker fetch over that ticker's own ``ranges`` (the whole
    ``[since, until]`` when it has none)."""

    def run(src: DataSource, ticker: str) -> list[Any]:
        spans = (ranges or {}).get(ticker) or [(since, until)]
        return [row for s, u in spans for row in fetch(src, ticker, s, u)]

    return run


def _status(ok: int, failed: int) -> str:
    if failed == 0:
        return "ok"
    if ok == 0:
        return "error"
    return "partial"


def _ratios(days: pd.Series, frame: pd.DataFrame) -> dict[date, float]:
    """``day -> adj_close / close`` of the rows where both are valid."""
    out: dict[date, float] = {}
    for day, close, adj in zip(days, frame["close"], frame["adj_close"], strict=True):
        ratio = adj_ratio(close, adj)
        if ratio is not None:
            out[cast(date, pd.Timestamp(day).date())] = ratio
    return out


def _prices_to_df(rows: Iterable[RawPriceBar]) -> pd.DataFrame:
    cols = ("ticker", "date", "open", "high", "low", "close", "adj_close", "volume")
    data = [tuple(getattr(r, c) for c in cols) for r in rows]
    return pd.DataFrame(data, columns=list(cols))


def _intraday_to_df(rows: Iterable[IntradayBar]) -> pd.DataFrame:
    cols = ("ticker", "timestamp", "open", "high", "low", "close", "adj_close", "volume")
    data = [tuple(getattr(r, c) for c in cols) for r in rows]
    frame = pd.DataFrame(data, columns=list(cols))
    # naive UTC, like the stored bars the quality checks compare against
    # (a vendor's aware stamps would not compare with them)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True).dt.tz_localize(None)
    return frame


def _macro_to_df(rows: Iterable[MacroIndicatorRow]) -> pd.DataFrame:
    cols = ("country_iso", "indicator", "observation_date", "period", "country_name", "value")
    data = [tuple(getattr(r, c) for c in cols) for r in rows]
    return pd.DataFrame(data, columns=list(cols))


def _defi_tvl_to_df(rows: Iterable[DefiTvlRow]) -> pd.DataFrame:
    cols = ("chain", "observation_date", "tvl_usd", "source")
    data = [tuple(getattr(r, c) for c in cols) for r in rows]
    return pd.DataFrame(data, columns=list(cols))


def _rows_to_df(rows: Iterable) -> pd.DataFrame:
    """Pydantic-row → DataFrame via ``model_dump``. Empty iterable → empty df."""
    data = [r.model_dump() for r in rows]
    if not data:
        return pd.DataFrame()
    return pd.DataFrame(data)


def _non_empty_parts(bundle: MetadataBundle) -> list[str]:
    out: list[str] = []
    if bundle.profile is not None:
        out.append("profile")
    if bundle.ticker_snapshot is not None:
        out.append("ticker_snapshot")
    if bundle.esg_snapshot is not None:
        out.append("esg_snapshot")
    for name in (
        "dividends",
        "splits",
        "insider_transactions",
        "news",
        "news_sentiment",
        "earnings_announcements",
        "analyst_forecasts",
        "analyst_ratings",
        "institutional_holders",
        "esg_activities",
        "cross_listings",
        "officers",
        "shares_outstanding",
        "employee_count",
        "segmentation",
        "market_cap_history",
    ):
        if getattr(bundle, name):
            out.append(name)
    return out
