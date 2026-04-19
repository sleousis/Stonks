"""Orchestrates fetch → normalize → idempotent upsert for a single DataSource.

Per-ticker failures are soft: logged, counted, and reflected in the
``ingest_runs`` row; the pipeline continues through the remaining tickers.
Final ``status`` is ``"ok"`` when all succeed, ``"partial"`` when at least one
succeeded, ``"error"`` when all failed, and always ``"ok"`` for an empty batch
(so callers can safely drive idempotent cron jobs).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date

import pandas as pd

from stonks.core.interval import Interval
from stonks.ingest.metadata_bundle import MetadataBundle
from stonks.ingest.schemas import FundamentalRow, IntradayBar, RawPriceBar
from stonks.ingest.sources.base import DataSource
from stonks.logging import get_logger
from stonks.store.lake import DuckDBLake


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
        return self._run(
            kind="fundamentals",
            tickers=tickers,
            fetch=self._source.fetch_fundamentals,
            to_df=_fundamentals_to_df,
            upsert=self._lake.upsert_fundamentals,
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
            except Exception as exc:
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
        if bundle.profile is not None:
            self._lake.upsert_ticker_profile(_rows_to_df([bundle.profile]))
        self._lake.upsert_dividends(_rows_to_df(bundle.dividends))
        self._lake.upsert_stock_splits(_rows_to_df(bundle.splits))
        self._lake.upsert_insider_transactions(_rows_to_df(bundle.insider_transactions))
        self._lake.upsert_news(_rows_to_df(bundle.news))
        self._lake.upsert_news_sentiment(_rows_to_df(bundle.news_sentiment))
        self._lake.upsert_analyst_estimates(_rows_to_df(bundle.analyst_estimates))
        if bundle.analyst_ratings is not None:
            self._lake.upsert_analyst_ratings(_rows_to_df([bundle.analyst_ratings]))
        self._lake.upsert_shares_outstanding(_rows_to_df(bundle.shares_outstanding))
        self._lake.upsert_employee_count(_rows_to_df(bundle.employee_count))
        self._lake.upsert_segmentation(_rows_to_df(bundle.segmentation))
        self._lake.upsert_market_cap_history(_rows_to_df(bundle.market_cap_history))

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
            except Exception as exc:  # per-ticker soft-fail
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


def _fundamentals_to_df(rows: Iterable[FundamentalRow]) -> pd.DataFrame:
    cols = ("ticker", "period_end", "frequency", "statement", "line_item", "value")
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
    for name in (
        "dividends", "splits", "insider_transactions", "news", "news_sentiment",
        "analyst_estimates", "shares_outstanding", "employee_count",
        "segmentation", "market_cap_history",
    ):
        if getattr(bundle, name):
            out.append(name)
    if bundle.analyst_ratings is not None:
        out.append("analyst_ratings")
    return out
