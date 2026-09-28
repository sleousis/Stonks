"""Streamed bars into the lake's :class:`~stonks.store.bars.BarStore`
(roadmap 21.1).

:class:`BarWriter` buffers closed bars and upserts them in batches. The
store keeps the last write per ``(ticker, timestamp, interval)``, so writing
a bar again (a replay, a restart, a REST backfill of the same minutes) never
duplicates it. Inside one batch the last bar per key wins too.

A failed write keeps the buffer, so the next flush tries again and no bar
is lost to a short store error. Timestamps are stored as naive UTC, like
every other bar.

DuckDB allows one writing process per lake file. Run the writer in the
process that owns the lake, or point it at the Parquet bar store, which
other processes can read while it writes.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

import pandas as pd

from stonks.core.interval import Interval
from stonks.core.stream import StreamBar
from stonks.store.bars import BAR_COLUMNS, BarStore


class BarWriter:
    def __init__(
        self,
        store: BarStore,
        *,
        interval: Interval = Interval.MIN_1,
        flush_bars: int = 500,
        flush_every: timedelta = timedelta(seconds=5),
    ) -> None:
        self.store = store
        self.interval = interval
        self.flush_bars = max(1, flush_bars)
        self.flush_every = flush_every
        self._buffer: dict[tuple[str, datetime], StreamBar] = {}
        self._last_flush: datetime | None = None
        self.bars_written = 0
        self.flushes = 0
        self.failed_flushes = 0

    @property
    def pending(self) -> int:
        return len(self._buffer)

    def add(self, bars: Iterable[StreamBar]) -> None:
        for bar in bars:
            if bar.interval != self.interval:
                raise ValueError(
                    f"this writer stores {self.interval.code} bars, got {bar.interval.code}"
                )
            self._buffer[(bar.ticker, bar.timestamp)] = bar

    def maybe_flush(self, now: datetime) -> int:
        """Flush when ``flush_bars`` are waiting or ``flush_every`` passed
        since the last flush. Returns how many bars were written."""
        if self._last_flush is None:
            self._last_flush = now
        if self.pending >= self.flush_bars or (
            self._buffer and now - self._last_flush >= self.flush_every
        ):
            written = self.flush()
            self._last_flush = now
            return written
        return 0

    def flush(self) -> int:
        """Write every buffered bar. Raises the store's error and keeps the
        buffer when the write fails."""
        if not self._buffer:
            return 0
        bars = list(self._buffer.values())
        try:
            self.store.upsert(bars_frame(bars, self.interval))
        except Exception:
            self.failed_flushes += 1
            raise
        self._buffer.clear()
        self.flushes += 1
        self.bars_written += len(bars)
        return len(bars)


def bars_frame(bars: Iterable[StreamBar], interval: Interval) -> pd.DataFrame:
    """``bars`` as a frame with :data:`~stonks.store.bars.BAR_COLUMNS`."""
    rows = [
        (
            b.ticker,
            b.timestamp.astimezone(UTC).replace(tzinfo=None),
            interval.code,
            b.open,
            b.high,
            b.low,
            b.close,
            b.adj_close,
            int(b.volume),
        )
        for b in bars
    ]
    frame = pd.DataFrame(rows, columns=list(BAR_COLUMNS))
    frame["timestamp"] = pd.to_datetime(frame["timestamp"])
    frame["volume"] = frame["volume"].astype("int64")
    return frame
