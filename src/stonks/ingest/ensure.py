"""On-demand data: make sure the lake holds bars for some tickers over a
window, fetching only what is missing.

:class:`DataEnsurer` works in three steps.

1. **Plan.** For each ticker it takes the window, clips it to today and to
   the source's known history limit (EODHD's free tier keeps one year),
   and subtracts what the lake already covers (first to last bar in the
   window) and the ranges the source was already asked for
   (``bar_fetch_ranges``, so a dead name with no data is not fetched on
   every run). Gaps that hold no closed session are dropped: weekends,
   holidays and today before the close, from the market calendar
   (:mod:`stonks.ingest.sessions`).
2. **Fetch.** Gaps are fetched concurrently on a bounded thread pool, each
   request waiting on a per-source :class:`RateLimiter`. When the plan
   allows bulk calls, an exchange whose tickers all miss the same few
   days is refreshed with one ``fetch_bulk_eod`` call a day, with a
   per-ticker fallback when the bulk call fails. Daily bars whose
   session has not closed yet are dropped, so they are asked for again
   after the close.
3. **Write.** The fetched rows go through
   :class:`~stonks.ingest.pipeline.IngestPipeline` on the calling thread,
   so there is one DuckDB writer, one ``ingest_runs`` row, the usual bar
   quality checks and per-ticker soft failure. A ticker whose fetch fails
   is retried on the pipeline's fallback source for its own gaps only.
   The ranges asked for are then recorded in ``bar_fetch_ranges``.

Adjustment drift. Vendors send ``adj_close`` adjusted as of the day of
the fetch, so bars fetched on different days can sit on different
bases. To keep one basis per ticker, a daily fetch starts
``overlap_bars`` stored bars before the trailing gap (in bulk mode: the
last stored day). When the vendor's ``adj_close / close`` on an
overlapping bar differs from the stored one by more than
``adjustment_tolerance``, a split or dividend happened since the last
fetch: the ticker's whole stored history is fetched again in the same
run. Bars older than the vendor's history limit cannot be fetched again,
so their ``adj_close`` is multiplied by the observed factor instead. The
overlap also overwrites a last bar that was stored before its session
closed.

Intraday holes inside the covered span are not detected; only the
leading and trailing gaps of each ticker are.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from typing import Any

import pandas as pd
from pydantic import BaseModel, Field

from stonks.core.interval import Interval
from stonks.ingest.adjustment import adj_ratio, adjustment_drift
from stonks.ingest.ensure_settings import EnsureSettings
from stonks.ingest.pipeline import _SOFT_FAIL_EXCEPTIONS, IngestPipeline
from stonks.ingest.redact import format_exception
from stonks.ingest.schemas import FinancialStatementsBundle, RawPriceBar
from stonks.ingest.sessions import SessionCloses, closed_sessions, default_sessions, is_final
from stonks.ingest.sources.base import DataSource, DataSourceError
from stonks.logging import get_logger
from stonks.store.lake import DuckDBLake

_log = get_logger("stonks.ingest.ensure")

DateRange = tuple[date, date]
PipelineFactory = Callable[[DataSource, DuckDBLake], IngestPipeline]


# ---- settings and vendor limits ------------------------------------------------------


@dataclass(frozen=True)
class VendorLimits:
    #: Oldest bar the plan serves, in days before today (``None``: no limit).
    max_history_days: int | None = None
    #: Whether the plan serves bulk (whole exchange) daily bars.
    bulk: bool = True
    #: Whether the plan serves intraday bars.
    intraday: bool = True


#: Known plan limits by (source id, plan). EODHD's free tier serves daily
#: prices for the last year only (CLAUDE.md, known external limits).
_KNOWN_LIMITS: dict[tuple[str, str], VendorLimits] = {
    ("eodhd", "free"): VendorLimits(max_history_days=365, bulk=False, intraday=False),
}


def vendor_limits(source_id: str, plan: str | None) -> VendorLimits:
    return _KNOWN_LIMITS.get((source_id, plan or ""), VendorLimits())


# ---- rate limiting -----------------------------------------------------------------------


class RateLimiter:
    """Thread-safe spacing of requests at ``rate`` per second (a burst of
    one). Shared by every thread fetching from one source."""

    def __init__(
        self,
        rate: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate <= 0:
            raise ValueError(f"rate must be > 0, got {rate}")
        self._interval = 1.0 / rate
        self._clock = clock
        self._sleep = sleep
        self._next = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = self._clock()
            start = max(now, self._next)
            self._next = start + self._interval
        wait = start - now
        if wait > 0:
            self._sleep(wait)


_LIMITERS: dict[tuple[str, float], RateLimiter] = {}
_LIMITERS_LOCK = threading.Lock()


def rate_limiter(source_id: str, rate: float) -> RateLimiter:
    """The process-wide limiter of ``source_id``, so concurrent ensures
    share one budget."""
    with _LIMITERS_LOCK:
        key = (source_id, rate)
        if key not in _LIMITERS:
            _LIMITERS[key] = RateLimiter(rate)
        return _LIMITERS[key]


# ---- gap arithmetic -------------------------------------------------------------------------


def missing_ranges(want: DateRange, have: Iterable[DateRange]) -> list[DateRange]:
    """Parts of the inclusive range ``want`` not covered by ``have``."""
    lo, hi = want
    out: list[DateRange] = []
    cursor = lo
    for s, e in sorted(have):
        if e < cursor or s > hi:
            continue
        if s > cursor:
            out.append((cursor, min(hi, s - timedelta(days=1))))
        cursor = max(cursor, e + timedelta(days=1))
        if cursor > hi:
            break
    if cursor <= hi:
        out.append((cursor, hi))
    return out


def _business_days(r: DateRange) -> list[date]:
    return [d.date() for d in pd.bdate_range(r[0], r[1])]


# ---- report ---------------------------------------------------------------------------------


class EnsureReport(BaseModel):
    """What an ensure did. ``run_id`` is the ``ingest_runs`` row (``None``
    when nothing was missing)."""

    run_id: int | None = None
    status: str = "ok"
    interval: str
    start: date
    end: date
    source: str
    tickers_requested: int = 0
    tickers_up_to_date: int = 0
    tickers_fetched: int = 0
    tickers_failed: int = 0
    gaps: int = 0
    bulk_days: int = 0
    #: The window start after the vendor's history limit, when it moved.
    clipped_start: date | None = None
    failed: list[str] = Field(default_factory=list)
    #: Tickers whose whole history was fetched again because the vendor's
    #: adjustment changed since the last fetch.
    readjusted: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


@dataclass
class _Plan:
    gaps: dict[str, list[DateRange]] = field(default_factory=dict)
    up_to_date: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    clipped_start: date | None = None
    start: date | None = None
    end: date | None = None
    #: Oldest date the vendor serves (``None``: no limit).
    oldest: date | None = None
    asset_classes: dict[str, str] = field(default_factory=dict)
    #: Stored ``adj_close / close`` of the last daily bars before each
    #: ticker's trailing gap, by date (the overlap re-fetched for drift).
    tails: dict[str, dict[date, float]] = field(default_factory=dict)
    #: First stored daily bar of each ticker with a tail.
    first_bar: dict[str, date] = field(default_factory=dict)
    #: Adjustment factor already seen (in the bulk overlap day) per ticker.
    drift: dict[str, float] = field(default_factory=dict)


# ---- the ensurer ----------------------------------------------------------------------------


class DataEnsurer:
    """See the module doc. ``pipeline_factory`` builds the write pipeline
    for a source and the lake; entrypoints pass
    :func:`stonks.ingest.wiring.build_ingest_pipeline` so quality checks,
    the fallback source and the notifier come from settings (default: a
    plain :class:`IngestPipeline`). ``today`` pins the date for tests
    (the clock then reads the end of that day), ``clock`` pins the instant
    (a timezone-aware UTC ``datetime``), and ``sessions`` gives the
    session closes (default: the market calendars)."""

    def __init__(
        self,
        lake: DuckDBLake,
        source: DataSource,
        settings: EnsureSettings | None = None,
        *,
        pipeline_factory: PipelineFactory | None = None,
        today: date | None = None,
        clock: Callable[[], datetime] | None = None,
        sessions: SessionCloses | None = None,
    ) -> None:
        self._lake = lake
        self._source = source
        self._settings = settings or EnsureSettings()
        self._pipeline_factory = pipeline_factory or (
            lambda src, lk: IngestPipeline(
                src, lk, adjustment_tolerance=self._settings.adjustment_tolerance
            )
        )
        self._today = today
        self._clock = clock
        self._sessions = sessions if sessions is not None else default_sessions()
        sid = source.source_id
        self._limits = vendor_limits(sid, self._settings.plans.get(sid))
        rate = self._settings.requests_per_second.get(
            sid, self._settings.default_requests_per_second
        )
        self._limiter = rate_limiter(sid, rate)

    @property
    def today(self) -> date:
        return self._today or self.now().date()

    def now(self) -> datetime:
        if self._clock is not None:
            return self._clock()
        if self._today is not None:
            return datetime.combine(self._today + timedelta(days=1), dtime(), UTC)
        return datetime.now(UTC)

    # ---- public ---------------------------------------------------------------

    def plan(
        self, tickers: Sequence[str], start: date, end: date, interval: Interval = Interval.DAY_1
    ) -> _Plan:
        tickers = list(dict.fromkeys(tickers))
        plan = _Plan()
        if interval.is_intraday and not self._limits.intraday:
            plan.warnings.append(
                f"the {self._source.source_id} plan serves no intraday bars: nothing fetched"
            )
            return plan
        end = min(end, self.today)
        if self._limits.max_history_days is not None:
            oldest = self.today - timedelta(days=self._limits.max_history_days)
            plan.oldest = oldest
            if start < oldest:
                plan.warnings.append(
                    f"the {self._source.source_id} plan keeps {self._limits.max_history_days} "
                    f"days of history: the window starts on {oldest} instead of {start}"
                )
                start = oldest
                plan.clipped_start = oldest
        plan.start, plan.end = start, end
        if start > end or not tickers:
            plan.up_to_date = tickers
            return plan
        covered = self._covered(tickers, start, end, interval)
        plan.asset_classes = self._lake.get_asset_classes(tickers)
        now = self.now()
        for t in tickers:
            gaps = missing_ranges((start, end), covered.get(t, []))
            if not interval.is_intraday:
                cls = plan.asset_classes.get(t)
                gaps = [g for g in gaps if closed_sessions(self._sessions, t, *g, now, cls)]
            if gaps:
                plan.gaps[t] = gaps
            else:
                plan.up_to_date.append(t)
        if interval == Interval.DAY_1 and self._settings.overlap_bars and plan.gaps:
            self._load_tails(plan)
        return plan

    def ensure(
        self,
        tickers: Sequence[str],
        start: date,
        end: date,
        interval: Interval = Interval.DAY_1,
    ) -> EnsureReport:
        """Fetch the missing ranges of ``tickers`` over ``[start, end]``."""
        tickers = list(dict.fromkeys(tickers))
        plan = self.plan(tickers, start, end, interval)
        report = EnsureReport(
            interval=str(interval),
            start=plan.start or start,
            end=plan.end or end,
            source=self._source.source_id,
            tickers_requested=len(tickers),
            tickers_up_to_date=len(plan.up_to_date),
            gaps=sum(len(g) for g in plan.gaps.values()),
            clipped_start=plan.clipped_start,
            warnings=list(plan.warnings),
        )
        for warning in plan.warnings:
            _log.warning("ensure.limit", source=self._source.source_id, warning=warning)
        if not plan.gaps:
            return report
        prefetched: dict[str, list[Any]] = {}
        if interval == Interval.DAY_1 and self._settings.bulk and self._limits.bulk:
            prefetched, report.bulk_days = self._bulk(plan)
        todo = [t for t in plan.gaps if t not in prefetched]
        return self._write(plan, prefetched, todo, interval, report)

    def refresh_exchange_day(
        self, exchange: str, day: date, tickers: Sequence[str] | None = None
    ) -> EnsureReport:
        """Refresh one exchange for one day: one bulk call, falling back to
        one call per ticker (``tickers``, else the source's listing) when
        the plan or the source has no bulk data."""
        report = EnsureReport(
            interval=str(Interval.DAY_1), start=day, end=day, source=self._source.source_id
        )
        rows: dict[str, list[Any]] = {}
        if self._limits.bulk:
            try:
                self._limiter.acquire()
                for bar in self._source.fetch_bulk_eod(exchange, day):
                    rows.setdefault(bar.ticker, []).append(bar)
                report.bulk_days = 1
            except _SOFT_FAIL_EXCEPTIONS as exc:
                report.warnings.append(f"bulk {exchange} {day} failed: {format_exception(exc)}")
                rows = {}
        if report.bulk_days:
            names = list(rows) if tickers is None else [t for t in tickers if t in rows]
            plan = _Plan(gaps={t: [(day, day)] for t in names})
            plan.asset_classes = self._lake.get_asset_classes(names)
            report.tickers_requested = len(names)
            report.gaps = len(names)
            return self._write(plan, {t: rows[t] for t in names}, [], Interval.DAY_1, report)
        names = list(tickers) if tickers is not None else self._source.list_tickers(exchange)
        plan = _Plan(gaps={t: [(day, day)] for t in names})
        plan.asset_classes = self._lake.get_asset_classes(names)
        report.tickers_requested = len(names)
        report.gaps = len(names)
        return self._write(plan, {}, names, Interval.DAY_1, report)

    # ---- internals -----------------------------------------------------------

    def _covered(
        self, tickers: list[str], start: date, end: date, interval: Interval
    ) -> dict[str, list[DateRange]]:
        out: dict[str, list[DateRange]] = {}
        cov = self._lake.bar_coverage(tickers, interval, start, end)
        for r in cov.itertuples(index=False):
            if int(r.n_window) > 0:
                out.setdefault(r.ticker, []).append(
                    (pd.Timestamp(r.first_bar).date(), pd.Timestamp(r.last_bar).date())
                )
        rows = self._lake.con.execute(
            """
            SELECT ticker, range_start, range_end FROM bar_fetch_ranges
             WHERE ticker = ANY(?) AND interval = ? AND range_end >= ? AND range_start <= ?
            """,
            [tickers, str(interval), start, end],
        ).fetchall()
        for t, s, e in rows:
            out.setdefault(t, []).append((pd.Timestamp(s).date(), pd.Timestamp(e).date()))
        return out

    def _load_tails(self, plan: _Plan) -> None:
        """Read the stored ``adj_close / close`` of the last
        ``overlap_bars`` daily bars before each ticker's trailing gap, and
        each such ticker's first stored daily bar."""
        names = list(plan.gaps)
        befores = [datetime.combine(plan.gaps[t][-1][0], dtime()) for t in names]
        rows = self._lake.con.execute(
            """
            WITH want AS (SELECT unnest(?) AS ticker, unnest(?) AS before)
            SELECT ticker, ts, close, adj_close FROM (
                SELECT b.ticker, b.timestamp AS ts, b.close, b.adj_close,
                       row_number() OVER (PARTITION BY b.ticker ORDER BY b.timestamp DESC) AS rn
                  FROM bars b JOIN want w ON b.ticker = w.ticker
                 WHERE b.interval = '1d' AND b.timestamp < w.before
            ) WHERE rn <= ?
            """,
            [names, befores, self._settings.overlap_bars],
        ).fetchall()
        for ticker, ts, close, adj in rows:
            ratio = adj_ratio(close, adj)
            if ratio is not None:
                plan.tails.setdefault(ticker, {})[pd.Timestamp(ts).date()] = ratio
        if not plan.tails:
            return
        firsts = self._lake.con.execute(
            "SELECT ticker, MIN(timestamp) FROM bars "
            "WHERE ticker = ANY(?) AND interval = '1d' GROUP BY ticker",
            [list(plan.tails)],
        ).fetchall()
        plan.first_bar = {t: pd.Timestamp(ts).date() for t, ts in firsts}

    def _drift(self, tail: dict[date, float], rows: Iterable[Any]) -> float | None:
        """The factor by which the vendor's ``adj_close / close`` moved on
        the oldest overlapping bar that moved, or ``None`` when none did."""
        fresh: dict[date, float] = {}
        for r in rows:
            ratio = adj_ratio(getattr(r, "close", None), getattr(r, "adj_close", None))
            if ratio is not None:
                fresh.setdefault(_row_day(r), ratio)
        return adjustment_drift(tail, fresh, self._settings.adjustment_tolerance)

    def _bulk(self, plan: _Plan) -> tuple[dict[str, list[Any]], int]:
        """Prefetched rows for tickers whose gaps a few bulk days cover.
        The last stored day is fetched too, so a changed adjustment shows;
        a ticker whose adjustment moved is left to the per-ticker path,
        which fetches its whole history."""
        gaps = plan.gaps
        by_exchange: dict[str, list[str]] = {}
        for t in gaps:
            if "." in t:
                by_exchange.setdefault(t.rsplit(".", 1)[1], []).append(t)
        out: dict[str, list[Any]] = {}
        calls = 0
        for exchange, names in by_exchange.items():
            if len(names) < self._settings.bulk_min_tickers:
                continue
            days = sorted({d for t in names for g in gaps[t] for d in _business_days(g)})
            if not days or len(days) > self._settings.bulk_max_days:
                continue
            tails = [max(plan.tails[t]) for t in names if plan.tails.get(t)]
            overlap = max(tails) if tails else None
            if overlap is not None and overlap not in days:
                days = sorted([*days, overlap])
            fetched = self._bulk_days(exchange, days)
            if fetched is None:
                continue
            calls += len(days)
            for t in names:
                rows = fetched.get(t, [])
                factor = self._drift(plan.tails.get(t, {}), rows)
                if factor is not None:
                    plan.drift[t] = factor
                    continue
                out[t] = rows
        return out, calls

    def _bulk_days(self, exchange: str, days: list[date]) -> dict[str, list[Any]] | None:
        def one(day: date) -> list[RawPriceBar]:
            self._limiter.acquire()
            return self._source.fetch_bulk_eod(exchange, day)

        rows: dict[str, list[Any]] = {}
        with ThreadPoolExecutor(
            max_workers=min(self._settings.max_workers, len(days)),
            thread_name_prefix="stonks-ensure-bulk",
        ) as pool:
            futures = [pool.submit(one, d) for d in days]
            try:
                for f in futures:
                    for bar in f.result():
                        rows.setdefault(bar.ticker, []).append(bar)
            except _SOFT_FAIL_EXCEPTIONS as exc:
                _log.warning(
                    "ensure.bulk_failed",
                    exchange=exchange,
                    error=format_exception(exc),
                    fallback="per ticker",
                )
                return None
        return rows

    def _fetch_ticker(
        self,
        ticker: str,
        plan: _Plan,
        interval: Interval,
        readjusted: dict[str, tuple[date, float]],
    ) -> list[Any]:
        """Fetch one ticker's gaps (plus the overlap for daily bars), or
        its whole history when its adjustment moved; drop daily bars whose
        session is still open."""
        gaps = plan.gaps[ticker]
        tail = plan.tails.get(ticker) if interval == Interval.DAY_1 else None
        factor = plan.drift.get(ticker)
        if factor is None:
            rows = self._fetch_ranges(ticker, _with_overlap(gaps, tail), interval)
            factor = self._drift(tail, rows) if tail else None
            if factor is None:
                return self._final(ticker, rows, interval, plan)
        start = plan.first_bar.get(ticker, gaps[0][0])
        if plan.oldest is not None:
            start = max(start, plan.oldest)
        start = min(start, gaps[0][0])
        rows = self._fetch_ranges(ticker, [(start, gaps[-1][1])], interval)
        readjusted[ticker] = (start, factor)
        _log.info("ensure.readjusted", ticker=ticker, since=str(start), factor=factor)
        return self._final(ticker, rows, interval, plan)

    def _final(self, ticker: str, rows: list[Any], interval: Interval, plan: _Plan) -> list[Any]:
        """``rows`` without the daily bars whose session has not closed."""
        if interval != Interval.DAY_1 or not rows:
            return rows
        keep = is_final(
            self._sessions,
            ticker,
            [_row_day(r) for r in rows],
            self.now(),
            plan.asset_classes.get(ticker),
        )
        return [r for r, ok in zip(rows, keep, strict=True) if ok]

    def _fetch_ranges(self, ticker: str, gaps: list[DateRange], interval: Interval) -> list[Any]:
        rows: list[Any] = []
        for since, until in gaps:
            self._limiter.acquire()
            if interval == Interval.DAY_1:
                rows.extend(self._source.fetch_prices(ticker, since=since, until=until))
            else:
                rows.extend(
                    self._source.fetch_intraday_bars(ticker, interval, since=since, until=until)
                )
        return rows

    def _write(
        self,
        plan: _Plan,
        prefetched: dict[str, list[Any]],
        todo: list[str],
        interval: Interval,
        report: EnsureReport,
    ) -> EnsureReport:
        gaps = plan.gaps
        tickers = list(gaps)
        fetched: dict[str, list[Any]] = {}
        readjusted: dict[str, tuple[date, float]] = {}
        ready = {t: self._final(t, rows, interval, plan) for t, rows in prefetched.items()}
        with ThreadPoolExecutor(
            max_workers=self._settings.max_workers, thread_name_prefix="stonks-ensure"
        ) as pool:
            prefetcher = _Prefetcher(
                pool,
                order=tickers,
                ready=ready,
                work=lambda t: self._fetch_ticker(t, plan, interval, readjusted),
                todo=set(todo),
                window=self._settings.prefetch_window,
                fetched=fetched,
            )
            source = _PrefetchedSource(self._source.source_id, prefetcher)
            pipeline = self._pipeline_factory(source, self._lake)
            # the prefetched source ignores the window; a fallback source
            # (retrying a failed ticker) is asked for that ticker's gaps only
            since = min(g[0] for gs in gaps.values() for g in gs)
            until = max(g[1] for gs in gaps.values() for g in gs)
            try:
                if interval == Interval.DAY_1:
                    # a fallback is asked for the overlap too, so the
                    # pipeline's drift check sees its basis (BE-36)
                    spans = {t: _with_overlap(g, plan.tails.get(t)) for t, g in gaps.items()}
                    result = pipeline.run_prices(tickers, since=since, until=until, ranges=spans)
                else:
                    result = pipeline.run_intraday_bars(
                        tickers, interval, since=since, until=until, ranges=gaps
                    )
            finally:
                prefetcher.cancel()
        # Stored bars older than a refetch (past the vendor's history
        # limit) were scaled onto the new basis by the pipeline (BE-09).
        # The pipeline's own per-ticker outcomes (BE-35): a ticker a
        # fallback rescued is not failed, and a failed write is.
        failed = [t for t in tickers if t in set(result.failed)]
        written = {t: rows for t, rows in fetched.items() if t not in set(failed)}
        self._record_ranges(gaps, written, interval)
        report.readjusted = sorted(t for t in readjusted if t in written)
        report.run_id = result.run_id
        report.status = result.status
        report.tickers_fetched = result.tickers_ok
        report.tickers_failed = result.tickers_failed
        report.failed = failed
        _log.info(
            "ensure.finished",
            run_id=result.run_id,
            source=self._source.source_id,
            interval=str(interval),
            fetched=result.tickers_ok,
            failed=result.tickers_failed,
            bulk_days=report.bulk_days,
        )
        return report

    def _record_ranges(
        self,
        gaps: dict[str, list[DateRange]],
        fetched: dict[str, list[Any]],
        interval: Interval,
    ) -> None:
        cutoff = self.today - timedelta(days=self._settings.settle_days)
        now = datetime.now(UTC).replace(tzinfo=None)
        records = []
        for ticker, rows in fetched.items():
            days = [_row_day(r) for r in rows]
            for since, until in gaps.get(ticker, []):
                end = until
                if until > cutoff:
                    last = max((d for d in days if since <= d <= until), default=None)
                    end = max(cutoff, last) if last is not None else cutoff
                    end = min(end, until)
                if end < since:
                    continue
                n = sum(1 for d in days if since <= d <= until)
                records.append((ticker, str(interval), since, end, self._source.source_id, n, now))
        if records:
            self._lake.con.executemany(
                """
                INSERT INTO bar_fetch_ranges VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO UPDATE SET rows = EXCLUDED.rows, fetched_at = EXCLUDED.fetched_at,
                                          source = EXCLUDED.source
                """,
                records,
            )


def _with_overlap(gaps: list[DateRange], tail: dict[date, float] | None) -> list[DateRange]:
    """``gaps`` with the last one stretched back over the stored ``tail``."""
    if not tail:
        return gaps
    return [*gaps[:-1], (min(tail), gaps[-1][1])]


def _row_day(row: Any) -> date:
    value = getattr(row, "date", None)
    if value is None:
        value = row.timestamp
    return value if type(value) is date else pd.Timestamp(value).date()


class _Prefetcher:
    """Fetch ``todo`` tickers on ``pool`` a bounded ``window`` ahead of the
    writer, which asks for them in ``order``. Successful results are kept
    in ``fetched`` (ticker -> rows) for the fetch log."""

    def __init__(
        self,
        pool: ThreadPoolExecutor,
        *,
        order: list[str],
        ready: dict[str, list[Any]],
        work: Callable[[str], list[Any]],
        todo: set[str],
        window: int,
        fetched: dict[str, list[Any]],
    ) -> None:
        self._pool = pool
        self._order = order
        self._ready = ready
        self._work = work
        self._todo = todo
        self._window = window
        self._fetched = fetched
        self._futures: dict[str, Future] = {}
        self._position = {t: i for i, t in enumerate(order)}
        self._next = 0
        self._submit_until(window)

    def _submit_until(self, index: int) -> None:
        while self._next < min(index, len(self._order)):
            t = self._order[self._next]
            self._next += 1
            if t in self._todo:
                self._futures[t] = self._pool.submit(self._work, t)

    def get(self, ticker: str) -> list[Any]:
        if ticker in self._ready:
            rows = self._ready.pop(ticker)
        else:
            if ticker not in self._futures:
                self._submit_until(self._position[ticker] + 1)
            future = self._futures.pop(ticker, None)
            if future is None:
                raise DataSourceError(f"{ticker} was not scheduled for fetching")
            try:
                rows = future.result()
            finally:
                self._submit_until(self._position[ticker] + 1 + self._window)
        self._fetched[ticker] = rows
        return rows

    def cancel(self) -> None:
        for future in self._futures.values():
            future.cancel()
        self._futures.clear()


class _PrefetchedSource(DataSource):
    """Serves the pipeline rows the ensurer already fetched, so the write
    path (validation, quarantine, soft failure, ``ingest_runs``) is the
    pipeline's own."""

    def __init__(self, source_id: str, prefetcher: _Prefetcher) -> None:
        self.source_id = source_id
        self._prefetcher = prefetcher

    def list_tickers(self, exchange: str) -> list[str]:
        raise DataSourceError("not available while ensuring data")

    def fetch_prices(self, ticker: str, since: date | None = None, until: date | None = None):
        return self._prefetcher.get(ticker)

    def fetch_intraday_bars(self, ticker, interval, since=None, until=None):
        return self._prefetcher.get(ticker)

    def fetch_fundamentals(self, ticker: str) -> FinancialStatementsBundle:
        raise DataSourceError("not available while ensuring data")
