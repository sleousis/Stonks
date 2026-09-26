"""Orchestrates fetch → normalize → idempotent upsert for a single DataSource.

Per-ticker failures are soft: logged, counted, and reflected in the
``ingest_runs`` row; the pipeline continues through the remaining tickers.
Final ``status`` is ``"ok"`` when all succeed, ``"partial"`` when at least one
succeeded, ``"error"`` when all failed, and always ``"ok"`` for an empty batch
(so callers can safely drive idempotent cron jobs).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from functools import partial
from typing import Any

import pandas as pd
import pydantic
import requests

from stonks.core.interval import Interval
from stonks.ingest.metadata_bundle import MetadataBundle
from stonks.ingest.redact import format_exception
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
    """Every ``run_*`` method funnels through :meth:`_run_units`, which owns
    the ``ingest_runs`` row lifecycle: open, per-unit soft-fail accounting,
    and a guaranteed close — even when a non-soft-fail exception (a lake
    error, a programmer bug, Ctrl-C) escapes the loop, in which case the row
    is closed as ``error`` and the exception re-raised. Operators never have
    to triage orphaned ``running`` rows by hand. The overall run is **not**
    atomic: units that succeeded before the failure stay committed.
    """

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
        return self._run_rows(
            kind="prices",
            tickers=tickers,
            fetch=lambda t: self._source.fetch_prices(t, since=since, until=until),
            to_df=_prices_to_df,
            upsert=self._lake.upsert_prices,
        )

    def run_intraday_bars(
        self,
        tickers: Sequence[str],
        interval: Interval,
        since: date | None = None,
        until: date | None = None,
    ) -> IngestRunResult:
        return self._run_rows(
            kind=f"intraday:{interval.code}",
            tickers=tickers,
            fetch=lambda t: self._source.fetch_intraday_bars(t, interval, since, until),
            to_df=_intraday_to_df,
            upsert=lambda df: self._lake.upsert_bars(df, interval=interval),
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

    def _run_rows(
        self,
        *,
        kind: str,
        tickers: Sequence[str],
        fetch: Callable[[str], Iterable[Any]],
        to_df: Callable[[Iterable[Any]], pd.DataFrame],
        upsert: Callable[[pd.DataFrame], Any],
    ) -> IngestRunResult:
        """Row-stream flavour of :meth:`_run_units`: fetch → DataFrame → upsert."""

        def ingest(ticker: str) -> dict[str, Any]:
            rows = list(fetch(ticker))
            upsert(to_df(rows))
            return {"rows": len(rows)}

        return self._run_units(
            kind=kind,
            units=[({"ticker": t}, partial(ingest, t)) for t in tickers],
        )

    def _run_units(
        self,
        *,
        kind: str,
        units: Iterable[tuple[dict[str, Any], Callable[[], dict[str, Any]]]],
        event: str = "ticker",
    ) -> IngestRunResult:
        """Run each ``(log_context, work)`` unit under one ``ingest_runs`` row.

        ``work()`` fetches + upserts one unit and returns extra fields for
        the success log line. Soft-fail exceptions count the unit as failed
        and move on; anything else closes the row as ``error`` and re-raises.
        Error text is credential-scrubbed before it is logged or stored.
        """
        run_id = self._lake.open_ingest_run(source=self._source.source_id, kind=kind)
        log = self._log.bind(run_id=run_id, kind=kind)

        ok = 0
        failed = 0
        last_error: str | None = None
        try:
            for context, work in units:
                try:
                    fields = work()
                except _SOFT_FAIL_EXCEPTIONS as exc:
                    failed += 1
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
