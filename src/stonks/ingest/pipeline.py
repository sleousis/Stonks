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
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from functools import partial
from typing import Any

import pandas as pd
import pydantic
import requests

from stonks.core.interval import Interval
from stonks.ingest.metadata_bundle import MetadataBundle
from stonks.ingest.quality import (
    BarQualityChecker,
    RunQuality,
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


@dataclass(frozen=True)
class IngestRunResult:
    run_id: int
    kind: str
    status: str  # "ok" | "partial" | "error"
    tickers_ok: int
    tickers_failed: int
    # Bar runs only: the quality summary stored on ``ingest_runs.quality_json``.
    quality: dict[str, Any] | None = None


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
    - retry a ticker whose primary fetch soft-fails on ``fallback`` (when
      given), recording the supplier in the run's quality summary; the
      ``ingest_runs`` row keeps the primary's id as its ``source``;
    - store the run's quality summary on ``ingest_runs.quality_json`` and
      send one warning through ``notifier`` when it breaches the
      checker's alert thresholds.
    """

    def __init__(
        self,
        source: DataSource,
        lake: DuckDBLake,
        *,
        quality: BarQualityChecker | None = None,
        fallback: DataSource | None = None,
        notifier: Notifier | None = None,
    ):
        self._source = source
        self._lake = lake
        self._quality = quality if quality is not None else BarQualityChecker()
        self._fallback = fallback
        self._notifier = notifier
        self._log = get_logger("stonks.ingest.pipeline").bind(source=source.source_id)

    def run_prices(
        self,
        tickers: Sequence[str],
        since: date | None = None,
        until: date | None = None,
    ) -> IngestRunResult:
        return self._run_bars(
            kind="prices",
            tickers=tickers,
            interval=Interval.DAY_1,
            fetch=lambda src, t: src.fetch_prices(t, since=since, until=until),
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
    ) -> IngestRunResult:
        return self._run_bars(
            kind=f"intraday:{interval.code}",
            tickers=tickers,
            interval=interval,
            fetch=lambda src, t: src.fetch_intraday_bars(t, interval, since, until),
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
    ) -> IngestRunResult:
        """Bar flavour of :meth:`_run_units`: fetch (with fallback) →
        DataFrame → validate → upsert the clean rows, quarantine the rest."""
        quality = RunQuality()
        run: dict[str, int] = {}
        stale_ref = as_of or datetime.now(UTC).date()

        def ingest(ticker: str) -> dict[str, Any]:
            rows, supplier = self._fetch_with_fallback(fetch, ticker)
            if supplier != self._source.source_id:
                quality.supplied_by[ticker] = supplier
            frame = to_df(rows)
            clean, quarantined = self._validate(
                frame, ticker, interval, stale_ref, run["id"], supplier, quality
            )
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

    def _fetch_with_fallback(
        self, fetch: Callable[[DataSource, str], Iterable[Any]], ticker: str
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
            return list(fetch(self._fallback, ticker)), fallback_id
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
        if frame.empty or not self._quality.config.enabled:
            quality.bars_checked += len(frame)
            return frame, 0
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
        return frame[~rejected], int(rejected.sum())

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
