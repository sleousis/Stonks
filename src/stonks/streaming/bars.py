"""Ticks to bars (roadmap 21.1).

:class:`BarBuilder` turns stream events into finished
:class:`~stonks.core.stream.StreamBar` rows with the columns of the lake's
``bars`` table (1m by default).

- A trade sets the bar's open (the earliest trade by time), high, low,
  close (the latest trade by time) and adds its size to the volume.
- A quote builds bars from its last price, else its mid, with volume 0.
  ``auto`` mode uses quotes only for tickers that never traded on this
  stream (forex feeds, IBKR snapshots), ``trades`` never, ``quotes`` always
  (and then ignores trades).
- A bar a source delivered itself passes through when its interval matches.
- A bar closes when a tick of a later bar of its ticker arrives, or once
  its end plus ``grace`` has passed (:meth:`BarBuilder.close_due`, also
  called on every heartbeat).
- A tick for a bar already closed (or older than the open one) is late. It
  is dropped and counted in :attr:`BarBuilder.late_ticks`, so a written bar
  never changes behind the writer's back.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from stonks.core.interval import Interval
from stonks.core.stream import (
    Heartbeat,
    QuoteTick,
    StreamBar,
    StreamEvent,
    TradeTick,
    bucket_start,
)
from stonks.streaming.settings import BarMode


@dataclass
class _OpenBar:
    start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    first_at: datetime
    last_at: datetime
    source: str

    def add(self, at: datetime, price: float, size: float) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        if at < self.first_at:
            self.first_at, self.open = at, price
        if at >= self.last_at:
            self.last_at, self.close = at, price
        self.volume += size


class BarBuilder:
    def __init__(
        self,
        *,
        interval: Interval = Interval.MIN_1,
        mode: BarMode = "auto",
        grace: timedelta = timedelta(seconds=2),
    ) -> None:
        if not interval.is_intraday:
            raise ValueError(f"bars are built at an intraday interval, got {interval.code}")
        self.interval = interval
        self.mode: BarMode = mode
        self.grace = grace
        self._step = interval.to_timedelta()
        self._open: dict[str, _OpenBar] = {}
        #: Per ticker, the start of the newest bar already closed.
        self._closed_through: dict[str, datetime] = {}
        self._traded: set[str] = set()
        self.late_ticks = 0
        self.ticks_used = 0

    # ---- input ----------------------------------------------------------------------

    def on_event(self, event: StreamEvent) -> list[StreamBar]:
        """Feed one event. Returns the bars it closed, oldest first."""
        if isinstance(event, Heartbeat):
            return self.close_due(event.timestamp)
        if isinstance(event, StreamBar):
            return self._pass_through(event)
        if isinstance(event, TradeTick):
            if self.mode == "quotes":
                return []
            self._traded.add(event.ticker)
            return self._add(event.ticker, event.timestamp, event.price, event.size, event.source)
        return self._quote(event)

    def _quote(self, q: QuoteTick) -> list[StreamBar]:
        if self.mode == "trades" or (self.mode == "auto" and q.ticker in self._traded):
            return []
        price = q.reference
        if price is None:
            return []
        return self._add(q.ticker, q.timestamp, price, 0.0, q.source)

    def _pass_through(self, bar: StreamBar) -> list[StreamBar]:
        if bar.interval != self.interval or self._is_late(bar.ticker, bar.timestamp):
            return []
        out: list[StreamBar] = []
        current = self._open.get(bar.ticker)
        if current is not None and current.start < bar.timestamp:
            out.append(self._close(bar.ticker))
        elif current is not None:
            # a vendor bar for the minute we were building wins
            del self._open[bar.ticker]
        self._closed_through[bar.ticker] = bar.timestamp
        out.append(bar)
        return out

    def _is_late(self, ticker: str, start: datetime) -> bool:
        closed = self._closed_through.get(ticker)
        current = self._open.get(ticker)
        return (closed is not None and start <= closed) or (
            current is not None and start < current.start
        )

    def _add(
        self, ticker: str, at: datetime, price: float, size: float, source: str
    ) -> list[StreamBar]:
        start = bucket_start(at, self.interval)
        if self._is_late(ticker, start):
            self.late_ticks += 1
            return []
        self.ticks_used += 1
        out: list[StreamBar] = []
        current = self._open.get(ticker)
        if current is not None and current.start < start:
            out.append(self._close(ticker))
            current = None
        if current is None:
            self._open[ticker] = _OpenBar(
                start, price, price, price, price, size, at, at, source or "stream"
            )
        else:
            current.add(at, price, size)
        return out

    # ---- closing --------------------------------------------------------------------

    def _close(self, ticker: str) -> StreamBar:
        bar = self._open.pop(ticker)
        self._closed_through[ticker] = bar.start
        return StreamBar(
            ticker=ticker,
            timestamp=bar.start,
            interval=self.interval,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=round(bar.volume),
            source=bar.source,
        )

    def close_due(self, now: datetime, *, grace: timedelta | None = None) -> list[StreamBar]:
        """Close every open bar whose end plus ``grace`` is at or before ``now``."""
        wait = self.grace if grace is None else grace
        due = [t for t, bar in self._open.items() if bar.start + self._step + wait <= now]
        return sorted((self._close(t) for t in due), key=lambda b: (b.timestamp, b.ticker))

    def flush(self, now: datetime) -> list[StreamBar]:
        """Close every bar whose minute has ended, without the grace period
        (on shutdown). The bar still in progress stays open."""
        return self.close_due(now, grace=timedelta(0))

    def drain(self) -> list[StreamBar]:
        """Close every open bar, finished or not (the end of a replay)."""
        return sorted(
            (self._close(t) for t in list(self._open)), key=lambda b: (b.timestamp, b.ticker)
        )

    def open_tickers(self) -> list[str]:
        return sorted(self._open)
