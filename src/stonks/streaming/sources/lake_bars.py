"""The ``lake_bars`` streaming source (roadmap 21.2.1).

Replays bars already in the lake as :class:`~stonks.core.stream.StreamBar`
events, so the intraday backtest runs on the same
:class:`~stonks.engine.driver.EventDriver` as a replay and the live stream.

- Bars come out in time order, then by ticker, so a run is deterministic.
- Point in time: a bar is yielded only once it has closed. With a
  :class:`~stonks.core.clock.FakeClock` the clock is set to the bar's close
  (its start plus the interval) before the bar is yielded. A bar that
  closes after ``end`` is never read (P12).
- The lake is read one ``chunk`` of time at a time, so a long backtest
  never holds every bar in memory.
- A stored row that is not a sound bar (high below close, say) is skipped
  and counted in :attr:`LakeBarSource.skipped_rows`.

It has no settings of its own: the intraday backtest builds it in code with
the lake, the window and the tickers.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, Self

import pandas as pd

from stonks.core.clock import FakeClock
from stonks.core.interval import Interval
from stonks.core.stream import StreamBar, StreamEvent
from stonks.logging import get_logger
from stonks.streaming.base import StreamConfigError, StreamContext, StreamingSource
from stonks.streaming.registry import register_stream_source

_log = get_logger("stonks.streaming.lake_bars")


class BarReader(Protocol):
    """What the source reads: :meth:`DuckDBLake.get_bars` fits."""

    def get_bars(self, ticker: str, interval: Interval, start: Any, end: Any) -> pd.DataFrame: ...


def _aware(when: datetime, what: str) -> datetime:
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError(f"{what} needs a timezone (got a naive datetime)")
    return when.astimezone(UTC)


def _naive(when: datetime) -> datetime:
    """The lake stores bar stamps as naive UTC."""
    return when.astimezone(UTC).replace(tzinfo=None)


def _stamp(value: Any) -> datetime:
    """A stored stamp (naive UTC, or aware) as an aware UTC datetime."""
    stamp = pd.Timestamp(value)
    aware = stamp.tz_localize(UTC) if stamp.tzinfo is None else stamp.tz_convert(UTC)
    when = aware.to_pydatetime()
    if pd.isna(when) or not isinstance(when, datetime):
        raise ValueError(f"a bar needs a timestamp, got {value!r}")
    return when


@register_stream_source("lake_bars")
class LakeBarSource(StreamingSource):
    finite = True

    def __init__(
        self,
        reader: BarReader,
        *,
        start: datetime,
        end: datetime,
        interval: Interval = Interval.MIN_1,
        clock: FakeClock | None = None,
        chunk: timedelta = timedelta(days=1),
    ) -> None:
        if not interval.is_intraday:
            raise ValueError(f"lake_bars replays intraday bars, got {interval.code}")
        self.start = _aware(start, "start")
        self.end = _aware(end, "end")
        if self.start >= self.end:
            raise ValueError("start must be before end")
        if chunk <= timedelta(0):
            raise ValueError("chunk must be positive")
        self.reader = reader
        self.interval = interval
        self.clock = clock
        self.chunk = chunk
        self.skipped_rows = 0
        self._step = interval.to_timedelta()
        self._stop = threading.Event()

    @classmethod
    def from_settings(cls, ctx: StreamContext) -> Self:
        raise StreamConfigError(
            "lake_bars has no settings: build it in code with the lake, a window and tickers"
        )

    def stream(self, tickers: Sequence[str]) -> Iterator[StreamEvent]:
        names = sorted(set(tickers))
        if not names:
            raise ValueError("lake_bars needs at least one ticker")
        self._stop.clear()
        # the last bar start whose bar closes by ``end``
        last_start = self.end - self._step
        lo = self.start
        while lo <= last_start:
            hi = min(lo + self.chunk, last_start + timedelta(microseconds=1))
            for event in self._chunk(names, lo, hi):
                if self._stop.is_set():
                    return
                if self.clock is not None:
                    self.clock.set(event.timestamp + self._step)
                yield event
            lo = hi

    def _chunk(self, tickers: list[str], lo: datetime, hi: datetime) -> list[StreamBar]:
        """Bars starting in ``[lo, hi)``, by time then ticker."""
        # the reader's window is inclusive, so ask up to the last whole
        # microsecond before ``hi`` and never past ``end``
        upper = _naive(hi - timedelta(microseconds=1))
        out: list[StreamBar] = []
        for ticker in tickers:
            frame = self.reader.get_bars(ticker, self.interval, _naive(lo), upper)
            out.extend(self._bars(ticker, frame))
        out.sort(key=lambda b: (b.timestamp, b.ticker))
        return out

    def _bars(self, ticker: str, frame: pd.DataFrame) -> Iterator[StreamBar]:
        for row in frame.to_dict("records"):
            volume = row["volume"]
            try:
                bar = StreamBar(
                    ticker=ticker,
                    timestamp=_stamp(row["timestamp"]),
                    interval=self.interval,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=0 if pd.isna(volume) else round(float(volume)),
                    source="lake",
                )
            except (TypeError, ValueError) as exc:
                self.skipped_rows += 1
                _log.warning(
                    "lake_bars.row_skipped",
                    ticker=ticker,
                    at=str(row["timestamp"]),
                    error=str(exc),
                )
                continue
            yield bar

    def close(self) -> None:
        self._stop.set()
