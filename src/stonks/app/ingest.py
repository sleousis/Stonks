"""IngestService — the ``ingest_runs`` ledger and triggering an ingest run
through :class:`IngestPipeline` with the configured data source."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal, Self

import pandas as pd
from pydantic import BaseModel, model_validator

from stonks.app.context import AppContext
from stonks.app.errors import ValidationError
from stonks.app.jobs import Job, JobContext, JobRunner
from stonks.app.pagination import Page
from stonks.core.interval import Interval
from stonks.ingest.pipeline import IngestPipeline, IngestRunResult
from stonks.ingest.sources.registry import (
    DEFAULT_SOURCE_ID,
    SOURCE_IDS,
    SourceConfigError,
    build_source,
)
from stonks.ingest.wiring import build_ingest_pipeline

INGEST_JOB = "ingest"

IngestKind = Literal["prices", "intraday", "fundamentals", "metadata"]
#: Mirrors ``stonks.ingest.sources.registry.SOURCE_IDS`` (a test pins it).
SourceId = Literal["eodhd", "yahoo", "defillama"]


class IngestRequest(BaseModel):
    kind: IngestKind
    #: Which configured data source to ingest from.
    source: SourceId = "eodhd"
    tickers: list[str] = []
    #: Fetch every ticker the source lists on this exchange (prices only).
    exchange: str | None = None
    since: date | None = None
    until: date | None = None
    #: Native intraday interval (``intraday`` only), e.g. ``5m``.
    interval: str | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not self.tickers and not self.exchange:
            raise ValueError("provide tickers or exchange")
        if self.exchange and self.kind != "prices":
            raise ValueError("exchange discovery is only supported for kind='prices'")
        if self.kind == "intraday" and not self.interval:
            raise ValueError("kind='intraday' requires interval")
        if self.since and self.until and self.since > self.until:
            raise ValueError("since must be on or before until")
        return self


class IngestRunView(BaseModel):
    id: int
    source: str
    kind: str
    started_at: datetime
    finished_at: datetime | None
    tickers_ok: int | None
    tickers_failed: int | None
    status: str | None
    error: str | None


class IngestResultView(BaseModel):
    run_id: int
    kind: str
    status: str
    tickers_ok: int
    tickers_failed: int


class DataSourceInfo(BaseModel):
    id: SourceId
    #: Used when an ingest request names no source.
    default: bool
    #: Whether the source can be built from the current settings.
    configured: bool
    #: Why it is not configured (e.g. a missing API key).
    detail: str | None = None


class IngestService:
    def __init__(self, context: AppContext, runner: JobRunner) -> None:
        self._ctx = context
        self._runner = runner
        # DuckDB write-write conflicts: one lake writer at a time.
        runner.register(INGEST_JOB, self._handle, lock="lake_write")

    def runs(
        self,
        *,
        kind: str | None = None,
        status: str | None = None,
        limit: int,
        offset: int,
    ) -> Page[IngestRunView]:
        where: list[str] = []
        params: list[Any] = []
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if status:
            where.append("status = ?")
            params.append(status)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        with self._ctx.lake() as lake:
            total = int(lake.sql(f"SELECT COUNT(*) AS n FROM ingest_runs{clause}", params).n[0])
            df = lake.sql(
                f"SELECT * FROM ingest_runs{clause} ORDER BY id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            )
        items = [IngestRunView(**_clean(r)) for r in df.to_dict(orient="records")]
        return Page[IngestRunView](items=items, total=total, limit=limit, offset=offset)

    def submit(self, request: IngestRequest) -> Job:
        self._validate(request)
        self._ctx.build_source(request.source)  # fail fast when it isn't configured
        return self._runner.submit(INGEST_JOB, request.model_dump(mode="json"))

    def run(self, request: IngestRequest, progress: JobContext | None = None) -> IngestResultView:
        self._validate(request)
        source = self._ctx.build_source(request.source)
        with self._ctx.lake() as lake:
            pipeline = build_ingest_pipeline(self._ctx.settings, source, lake)
            tickers = list(request.tickers)
            if not tickers and request.exchange:
                tickers = source.list_tickers(request.exchange)
                if progress is not None:
                    progress.progress(0.1, f"{len(tickers)} tickers on {request.exchange}")
            result = self._dispatch(pipeline, request, tickers)
        return IngestResultView(
            run_id=result.run_id,
            kind=result.kind,
            status=result.status,
            tickers_ok=result.tickers_ok,
            tickers_failed=result.tickers_failed,
        )

    def sources(self) -> list[DataSourceInfo]:
        """Every data source the registry knows and whether it is usable.
        Builds each source (no network) to find out; the configured test
        override (``source_factory``) is ignored here."""
        out: list[DataSourceInfo] = []
        for sid in SOURCE_IDS:
            try:
                build_source(sid, self._ctx.settings.sources)
            except SourceConfigError as exc:
                configured, detail = False, str(exc)
            except Exception as exc:  # e.g. a vendor library failing to import
                configured, detail = False, f"{type(exc).__name__}: {exc}"
            else:
                configured, detail = True, None
            out.append(
                DataSourceInfo(
                    id=sid,  # type: ignore[arg-type]
                    default=sid == DEFAULT_SOURCE_ID,
                    configured=configured,
                    detail=detail,
                )
            )
        return out

    def _dispatch(
        self, pipeline: IngestPipeline, req: IngestRequest, tickers: list[str]
    ) -> IngestRunResult:
        if req.kind == "prices":
            return pipeline.run_prices(tickers, since=req.since, until=req.until)
        if req.kind == "intraday":
            return pipeline.run_intraday_bars(
                tickers, Interval.parse(req.interval or ""), since=req.since, until=req.until
            )
        if req.kind == "fundamentals":
            return pipeline.run_fundamentals(tickers)
        return pipeline.run_metadata(tickers)

    def _validate(self, req: IngestRequest) -> None:
        if req.interval is not None:
            try:
                Interval.parse(req.interval)
            except (ValueError, TypeError) as exc:
                raise ValidationError(f"invalid interval {req.interval!r}: {exc}") from None

    def _handle(self, params: dict[str, Any], ctx: JobContext) -> IngestResultView:
        return self.run(IngestRequest.model_validate(params), progress=ctx)


def _clean(record: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in record.items():
        if v is None or (not isinstance(v, str) and pd.isna(v)):
            out[k] = None
        elif isinstance(v, pd.Timestamp):
            out[k] = v.to_pydatetime()
        else:
            out[k] = v
    return out
