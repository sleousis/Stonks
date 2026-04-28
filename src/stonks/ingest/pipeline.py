"""Orchestrates fetch → normalize → idempotent upsert for a single DataSource.

Per-ticker failures are soft: logged, counted, and reflected in the
``ingest_runs`` row; the pipeline continues through the remaining tickers.
Final ``status`` is ``"ok"`` when all succeed, ``"partial"`` when at least one
succeeded, ``"error"`` when all failed, and always ``"ok"`` for an empty batch
(so callers can safely drive idempotent cron jobs).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date

import pandas as pd
import pydantic
import requests

from stonks.core.interval import Interval
from stonks.ingest.metadata_bundle import MetadataBundle
from stonks.ingest.schemas import (
    FinancialStatementsBundle,
    IntradayBar,
    MacroIndicatorRow,
    RawPriceBar,
)
from stonks.ingest.sources.base import DataSource, DataSourceError
from stonks.logging import get_logger
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


@dataclass(frozen=True)
class IngestRunResult:
    run_id: int
    kind: str
    status: str  # "ok" | "partial" | "error"
    tickers_ok: int
    tickers_failed: int


class IngestPipeline:
    def __init__(self, source: DataSource, lake: DuckDBLake):
        self._source = source
        self._lake = lake
        self._log = get_logger("stonks.ingest.pipeline").bind(source=source.source_id)

    def run_prices(
        self,
        tickers: Sequence[str],
        since: date | None = None,
        until: date | None = None,
    ) -> IngestRunResult:
        return self._run(
            kind="prices",
            tickers=tickers,
            fetch=lambda t: self._source.fetch_prices(t, since=since, until=until),
            to_df=_prices_to_df,
            upsert=self._lake.upsert_prices,
        )

    def run_fundamentals(self, tickers: Sequence[str]) -> IngestRunResult:
        """Pull each ticker's :class:`FinancialStatementsBundle` and upsert
        the three statements into their dedicated tables.

        One vendor call per ticker (the bundle), three lake writes
        wrapped in a per-ticker transaction so a partial failure on one
        ticker never leaves that ticker half-populated. The overall run
        is **not** atomic — successful tickers stay committed even if a
        later ticker fails. The ``ingest_runs`` row is always closed,
        even if an unhandled exception escapes the loop, so operators
        never have to clean up orphaned ``running`` rows by hand.
        """
        run_id = self._lake.open_ingest_run(source=self._source.source_id, kind="fundamentals")
        log = self._log.bind(run_id=run_id, kind="fundamentals")

        ok = 0
        failed = 0
        last_error: str | None = None
        try:
            for ticker in tickers:
                try:
                    bundle = self._source.fetch_fundamentals(ticker)
                    self._upsert_statements_bundle(bundle)
                    ok += 1
                    log.info(
                        "ticker.ingested",
                        ticker=ticker,
                        income_rows=len(bundle.income),
                        balance_rows=len(bundle.balance),
                        cashflow_rows=len(bundle.cashflow),
                    )
                except _SOFT_FAIL_EXCEPTIONS as exc:
                    failed += 1
                    last_error = f"{type(exc).__name__}: {exc}"
                    log.warning("ticker.failed", ticker=ticker, error=last_error)

            status = _status(ok, failed)
        except BaseException as exc:
            # Catch SystemExit / KeyboardInterrupt too: the ingest_runs
            # row must close so operators don't have to triage orphaned
            # ``running`` rows. Re-raised after the row is closed.
            status = "error"
            last_error = f"{type(exc).__name__}: {exc}"
            self._lake.close_ingest_run(
                run_id,
                tickers_ok=ok,
                tickers_failed=failed,
                status=status,
                error=last_error,
            )
            log.error("run.aborted", status=status, error=last_error)
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
            kind="fundamentals",
            status=status,
            tickers_ok=ok,
            tickers_failed=failed,
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
        run_id = self._lake.open_ingest_run(source=self._source.source_id, kind="macro")
        log = self._log.bind(run_id=run_id, kind="macro")

        ok = 0
        failed = 0
        last_error: str | None = None

        for country in countries:
            for indicator in indicators:
                try:
                    rows = list(self._source.fetch_macro_indicator(country, indicator))
                    df = _macro_to_df(rows)
                    self._lake.upsert_macro_indicators(df)
                    ok += 1
                    log.info(
                        "macro.ingested",
                        country_iso=country,
                        indicator=indicator,
                        rows=len(rows),
                    )
                except _SOFT_FAIL_EXCEPTIONS as exc:
                    failed += 1
                    last_error = f"{type(exc).__name__}: {exc}"
                    log.warning(
                        "macro.failed",
                        country_iso=country,
                        indicator=indicator,
                        error=last_error,
                    )

        status = _status(ok, failed)
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
            kind="macro",
            status=status,
            tickers_ok=ok,
            tickers_failed=failed,
        )

    def run_intraday_bars(
        self,
        tickers: Sequence[str],
        interval: Interval,
        since: date | None = None,
        until: date | None = None,
    ) -> IngestRunResult:
        return self._run(
            kind=f"intraday:{interval.code}",
            tickers=tickers,
            fetch=lambda t: self._source.fetch_intraday_bars(t, interval, since, until),
            to_df=_intraday_to_df,
            upsert=lambda df: self._lake.upsert_bars(df, interval=interval),
        )

    def run_metadata(self, tickers: Sequence[str]) -> IngestRunResult:
        """Pull the extended fundamentals bundle per ticker (profile, dividends,
        insiders, news + sentiment, analyst estimates + ratings, shares
        outstanding, employee count, segmentations) and upsert each non-empty
        part into its matching lake table."""
        run_id = self._lake.open_ingest_run(source=self._source.source_id, kind="metadata")
        log = self._log.bind(run_id=run_id, kind="metadata")

        ok = 0
        failed = 0
        last_error: str | None = None

        for ticker in tickers:
            try:
                bundle = self._source.fetch_metadata(ticker)
                self._upsert_bundle(bundle)
                ok += 1
                log.info(
                    "ticker.ingested",
                    ticker=ticker,
                    kinds_found=_non_empty_parts(bundle),
                )
            except _SOFT_FAIL_EXCEPTIONS as exc:
                failed += 1
                last_error = f"{type(exc).__name__}: {exc}"
                log.warning("ticker.failed", ticker=ticker, error=last_error)

        status = _status(ok, failed)
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
            kind="metadata",
            status=status,
            tickers_ok=ok,
            tickers_failed=failed,
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

    def _run(self, *, kind, tickers, fetch, to_df, upsert) -> IngestRunResult:
        run_id = self._lake.open_ingest_run(source=self._source.source_id, kind=kind)
        log = self._log.bind(run_id=run_id, kind=kind)

        ok = 0
        failed = 0
        last_error: str | None = None

        for ticker in tickers:
            try:
                rows = list(fetch(ticker))
                df = to_df(rows)
                upsert(df)
                ok += 1
                log.info("ticker.ingested", ticker=ticker, rows=len(rows))
            except _SOFT_FAIL_EXCEPTIONS as exc:  # per-ticker soft-fail
                failed += 1
                last_error = f"{type(exc).__name__}: {exc}"
                log.warning("ticker.failed", ticker=ticker, error=last_error)

        status = _status(ok, failed)
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
        )


def _status(ok: int, failed: int) -> str:
    if failed == 0:
        return "ok"
    if ok == 0:
        return "error"
    return "partial"


def _prices_to_df(rows: Iterable[RawPriceBar]) -> pd.DataFrame:
    cols = ("ticker", "date", "open", "high", "low", "close", "adj_close", "volume")
    data = [tuple(getattr(r, c) for c in cols) for r in rows]
    return pd.DataFrame(data, columns=list(cols))


def _intraday_to_df(rows: Iterable[IntradayBar]) -> pd.DataFrame:
    cols = ("ticker", "timestamp", "open", "high", "low", "close", "adj_close", "volume")
    data = [tuple(getattr(r, c) for c in cols) for r in rows]
    return pd.DataFrame(data, columns=list(cols))


def _macro_to_df(rows: Iterable[MacroIndicatorRow]) -> pd.DataFrame:
    cols = ("country_iso", "indicator", "observation_date", "period", "country_name", "value")
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
