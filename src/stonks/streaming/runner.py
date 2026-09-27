"""The supervised stream runner (roadmap 21.1).

:class:`StreamRunner` keeps one :class:`~stonks.streaming.base.StreamingSource`
flowing into the bar store:

1. Every event goes to the recorder (when one is set), the subscribers
   (the event engine of 21.2, price alerts), and the
   :class:`~stonks.streaming.bars.BarBuilder`. Closed bars go to the
   :class:`~stonks.streaming.writer.BarWriter` and the bar subscribers.
2. A lost connection is retried with exponential backoff and jitter
   (:class:`Backoff`, reset once a connection delivers). A refused login
   (:class:`~stonks.streaming.base.StreamAuthError`) is never retried.
3. A stream that stays silent for ``stale_after_seconds`` while the market
   is open counts as lost, since a half-open socket can look alive.
4. Gaps: the time between the last event and the reconnect, and at startup
   the time since today's open. Once data flows again (and
   ``backfill_delay_seconds`` later, so the vendor has finished the minute),
   the gap is backfilled through the REST intraday ingest
   (:class:`PipelineBackfiller`). Writes are idempotent, so the backfill and
   the stream never duplicate a bar.
5. :class:`~stonks.streaming.health.StreamHealth` records all of it.

A subscriber that raises is counted and logged, never allowed to stop the
stream. A failed bar write keeps its bars for the next flush. On stop the
runner writes every bar whose minute has ended (the minute in progress is
left to the next run or backfill), and at the end of a finite source
(a replay) every bar.

Time comes from the :class:`~stonks.core.clock.Clock`. A replay drives a
``FakeClock``, so the runner behaves the same on recorded and live data.
"""

from __future__ import annotations

import random
import threading
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from stonks.core.clock import SYSTEM_CLOCK, Clock
from stonks.core.interval import Interval
from stonks.core.stream import Heartbeat, StreamBar, StreamEvent
from stonks.logging import get_logger
from stonks.streaming.bars import BarBuilder
from stonks.streaming.base import (
    StreamAuthError,
    StreamContext,
    StreamDisconnectedError,
    StreamError,
    StreamingSource,
)
from stonks.streaming.health import Gap, GapReason, StreamHealth
from stonks.streaming.recorder import StreamRecorder
from stonks.streaming.settings import StreamBackoffSettings, StreamingSettings
from stonks.streaming.writer import BarWriter

if TYPE_CHECKING:
    from stonks.config import Settings
    from stonks.ingest.pipeline import IngestPipeline, IngestRunResult
    from stonks.store.bars import BarStore
    from stonks.store.lake import DuckDBLake
    from stonks.store.state import SqliteState

_log = get_logger("stonks.streaming.runner")

EventSubscriber = Callable[[StreamEvent], object]
BarSubscriber = Callable[[StreamBar], object]


class _SessionLike(Protocol):
    @property
    def open(self) -> datetime: ...

    @property
    def close(self) -> datetime: ...


class MarketHours(Protocol):
    """What the runner asks a market calendar (``scheduling.calendar``)."""

    def is_open_at(self, when: datetime) -> bool: ...

    def session(self, day: date) -> _SessionLike | None: ...


class Backfiller(Protocol):
    def backfill(self, tickers: Sequence[str], start: datetime, end: datetime) -> int:
        """Fetch ``tickers``' bars over ``[start, end]`` into the store.
        Returns how many tickers came back. Raises when none did."""
        ...


class _StaleError(StreamDisconnectedError):
    """No data for too long while the market is open."""


class Backoff:
    """``initial * multiplier ** n`` seconds, capped at ``max``, each spread
    by up to ``jitter`` of itself."""

    def __init__(self, settings: StreamBackoffSettings, *, rng: random.Random | None = None):
        self.settings = settings
        self._rng = rng or random.Random()
        self._attempt = 0

    def next_delay(self) -> float:
        s = self.settings
        base = min(s.max_seconds, s.initial_seconds * s.multiplier**self._attempt)
        self._attempt += 1
        if s.jitter:
            base *= 1.0 + self._rng.uniform(-s.jitter, s.jitter)
        return base

    def reset(self) -> None:
        self._attempt = 0


class PipelineBackfiller:
    """Backfills through :meth:`IngestPipeline.run_intraday_bars`, which
    fetches whole UTC days, checks quality and records an ``ingest_runs`` row."""

    def __init__(self, pipeline: IngestPipeline, interval: Interval = Interval.MIN_1) -> None:
        self.pipeline = pipeline
        self.interval = interval

    def backfill(self, tickers: Sequence[str], start: datetime, end: datetime) -> int:
        result: IngestRunResult = self.pipeline.run_intraday_bars(
            list(tickers), self.interval, since=start.date(), until=end.date()
        )
        if result.tickers_ok == 0 and result.tickers_failed > 0:
            raise StreamError(f"the REST backfill failed for every ticker ({result.status})")
        return result.tickers_ok


class StreamRunner:
    def __init__(
        self,
        source: StreamingSource,
        settings: StreamingSettings,
        *,
        tickers: Sequence[str] | None = None,
        store: BarStore | None = None,
        recorder: StreamRecorder | None = None,
        backfiller: Backfiller | None = None,
        hours: MarketHours | None = None,
        clock: Clock = SYSTEM_CLOCK,
        sleep: Callable[[float], object] | None = None,
        rng: random.Random | None = None,
        subscribers: Sequence[EventSubscriber] = (),
        bar_subscribers: Sequence[BarSubscriber] = (),
        max_reconnects: int | None = None,
    ) -> None:
        self.source = source
        self.settings = settings
        self.tickers = list(settings.tickers if tickers is None else tickers)
        self.builder = BarBuilder(
            mode=settings.bar_mode, grace=timedelta(seconds=settings.bar_grace_seconds)
        )
        self.writer = (
            BarWriter(
                store,
                flush_bars=settings.flush_bars,
                flush_every=timedelta(seconds=settings.flush_seconds),
            )
            if store is not None
            else None
        )
        self.recorder = recorder
        self.backfiller = backfiller if settings.backfill else None
        self.hours = hours
        self.clock = clock
        self.backoff = Backoff(settings.backoff, rng=rng)
        self.subscribers = list(subscribers)
        self.bar_subscribers = list(bar_subscribers)
        self.max_reconnects = max_reconnects
        self.health = StreamHealth(source=source.source_id or type(source).__name__)
        self._stop = threading.Event()
        self._sleep: Callable[[float], object] = sleep or self._stop.wait
        self._stale_after = timedelta(seconds=settings.stale_after_seconds)
        self._min_gap = timedelta(seconds=settings.min_gap_seconds)
        self._backfill_delay = timedelta(seconds=settings.backfill_delay_seconds)
        self._gap: Gap | None = None

    # ---- control --------------------------------------------------------------------

    def stop(self) -> None:
        """End :meth:`run` soon. Safe from another thread."""
        self._stop.set()
        self.source.close()

    def run(self) -> StreamHealth:
        """Stream until :meth:`stop`, the end of a finite source, a refused
        login (raised) or too many reconnects (raised)."""
        self._stop.clear()
        self.health.started_at = self.clock.now()
        self._startup_gap(self.health.started_at)
        finite_end = False
        try:
            while not self._stop.is_set():
                self.health.state = "connecting"
                try:
                    self._consume()
                    if self.source.finite:
                        finite_end = True
                        break
                    if self._stop.is_set():
                        break
                    raise StreamDisconnectedError("the stream ended")
                except StreamAuthError as exc:
                    self.health.state = "failed"
                    self.health.last_error = str(exc)
                    _log.error("stream.auth_failed", source=self.health.source, error=str(exc))
                    raise
                except (StreamDisconnectedError, OSError, TimeoutError) as exc:
                    if self._stop.is_set():
                        break
                    self._on_disconnect(exc)
            return self.health
        finally:
            self._shutdown(drain=finite_end)

    # ---- the loop -------------------------------------------------------------------

    def _consume(self) -> None:
        connected = False
        for event in self.source.stream(self.tickers):
            if self._stop.is_set():
                return
            now = self.clock.now()
            if not connected:
                connected = True
                self._on_connect(now)
            self._handle(event, now)
            self._maybe_backfill(now)
            self._check_stale(now)

    def _on_connect(self, now: datetime) -> None:
        self.health.connects += 1
        self.health.connected_at = now
        self.health.state = "streaming"
        self.backoff.reset()
        if self._gap is not None and self._gap.end is None:
            self._gap.end = now
        _log.info("stream.connected", source=self.health.source, tickers=len(self.tickers))

    def _on_disconnect(self, exc: BaseException) -> None:
        h = self.health
        now = self.clock.now()
        h.disconnects += 1
        h.last_error = str(exc) or type(exc).__name__
        reason: GapReason = "stale" if isinstance(exc, _StaleError) else "disconnect"
        start = h.last_event_at or h.connected_at or now
        if self._gap is None:
            self._gap = Gap(start, None, reason)
            h.add_gap(self._gap)
        else:  # still down since an earlier gap: it just gets longer
            self._gap.start = min(self._gap.start, start)
            self._gap.end = None
        _log.warning(
            "stream.disconnected",
            source=h.source,
            reason=reason,
            error=h.last_error,
            disconnects=h.disconnects,
        )
        if self.max_reconnects is not None and h.disconnects > self.max_reconnects:
            h.state = "failed"
            raise StreamError(
                f"the {h.source} stream gave up after {self.max_reconnects} reconnects"
            ) from exc
        h.state = "backoff"
        self._sleep(self.backoff.next_delay())

    def _handle(self, event: StreamEvent, now: datetime) -> None:
        self.health.count(event, now)
        if self.recorder is not None:
            self.recorder.write(event)
        for subscriber in self.subscribers:
            self._notify(subscriber, event)
        bars = self.builder.on_event(event)
        if not isinstance(event, Heartbeat):
            bars += self.builder.close_due(now)
        self.health.late_ticks = self.builder.late_ticks
        self._emit(bars)
        if self.writer is not None:
            try:
                self.writer.maybe_flush(now)
            except Exception as exc:  # the buffer stays for the next flush
                self._write_failed(exc)

    def _emit(self, bars: list[StreamBar]) -> None:
        if not bars:
            return
        if self.writer is not None:
            self.writer.add(bars)
        for bar in bars:
            for subscriber in self.bar_subscribers:
                self._notify(subscriber, bar)

    def _notify[T](self, subscriber: Callable[[T], object], item: T) -> None:
        try:
            subscriber(item)
        except Exception as exc:
            self.health.subscriber_errors += 1
            _log.warning("stream.subscriber_failed", error=str(exc), kind=type(item).__name__)

    def _write_failed(self, exc: BaseException) -> None:
        self.health.write_errors += 1
        self.health.last_error = f"bar write failed: {exc}"
        _log.warning("stream.write_failed", error=str(exc))

    def _check_stale(self, now: datetime) -> None:
        if self.hours is None or not self.hours.is_open_at(now):
            return
        marks = [t for t in (self.health.last_event_at, self.health.connected_at) if t]
        if marks and now - max(marks) > self._stale_after:
            raise _StaleError(f"no data for {self.settings.stale_after_seconds:g} s")

    # ---- gaps -----------------------------------------------------------------------

    def _startup_gap(self, now: datetime) -> None:
        if self.backfiller is None or self.hours is None:
            return
        session = self.hours.session(now.date())
        if session is not None and session.open < now < session.close:
            self._gap = Gap(session.open, None, "startup")
            self.health.add_gap(self._gap)

    def _maybe_backfill(self, now: datetime, *, force: bool = False) -> None:
        gap = self._gap
        if gap is None or gap.end is None:
            return
        if not force and now < gap.end + self._backfill_delay:
            return
        self._gap = None
        if self.backfiller is None or gap.end - gap.start < self._min_gap:
            return
        if self.writer is not None:
            # stream bars first, so the vendor's bars land last
            try:
                self.writer.flush()
            except Exception as exc:
                self._write_failed(exc)
        try:
            self.backfiller.backfill(self.tickers, gap.start, gap.end)
        except Exception as exc:
            gap.backfilled = False
            self.health.backfills_failed += 1
            self.health.last_error = f"backfill failed: {exc}"
            _log.warning("stream.backfill_failed", error=str(exc), reason=gap.reason)
            return
        gap.backfilled = True
        self.health.backfills_ok += 1
        _log.info(
            "stream.backfilled",
            reason=gap.reason,
            start=gap.start.isoformat(),
            end=gap.end.isoformat(),
        )

    # ---- shutdown -------------------------------------------------------------------

    def _shutdown(self, *, drain: bool) -> None:
        now = self.clock.now()
        self._emit(self.builder.drain() if drain else self.builder.flush(now))
        self._maybe_backfill(now, force=True)
        if self.writer is not None:
            try:
                self.writer.flush()
            except Exception as exc:
                self._write_failed(exc)
            self.health.bars_written = self.writer.bars_written
        if self.recorder is not None:
            self.recorder.close()
        self.source.close()
        if self.health.state != "failed":
            self.health.state = "stopped"
        _log.info("stream.stopped", **self.health.snapshot()["backfills"])


def build_runner(
    settings: Settings,
    *,
    lake: DuckDBLake | None = None,
    store: BarStore | None = None,
    state: SqliteState | None = None,
    clock: Clock = SYSTEM_CLOCK,
    source_id: str | None = None,
    record: bool | None = None,
) -> StreamRunner:
    """A runner from ``[streaming]``: the source from the registry, the bar
    store of ``lake`` (or ``store``), the recorder when ``[streaming.record]``
    is on, the REST backfill when ``backfill`` is on and its source builds,
    and the market hours of ``calendar``."""
    from stonks.ingest.sources.registry import SourceConfigError, build_source
    from stonks.ingest.wiring import build_ingest_pipeline
    from stonks.scheduling.calendar import get_calendar
    from stonks.streaming.registry import build_stream_source

    cfg = settings.streaming
    source = build_stream_source(StreamContext(settings, clock=clock, state=state), source_id)
    bar_store = store if store is not None else (lake.bar_store if lake is not None else None)
    recorder = None
    if cfg.record.enabled if record is None else record:
        recorder = StreamRecorder(
            cfg.record.dir,
            chunk_events=cfg.record.chunk_events,
            chunk_every=timedelta(seconds=cfg.record.chunk_seconds),
            clock=clock,
        )
    backfiller: Backfiller | None = None
    if cfg.backfill and lake is not None:
        try:
            rest = build_source(cfg.backfill_source, settings.sources)
            backfiller = PipelineBackfiller(build_ingest_pipeline(settings, rest, lake))
        except SourceConfigError as exc:
            _log.warning("stream.backfill_off", error=str(exc))
    return StreamRunner(
        source,
        cfg,
        store=bar_store,
        recorder=recorder,
        backfiller=backfiller,
        hours=get_calendar(cfg.calendar),
        clock=clock,
    )
