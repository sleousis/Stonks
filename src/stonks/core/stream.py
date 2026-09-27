"""Stream events: what a live price feed says, vendor neutral (roadmap 21.1).

Every :class:`~stonks.streaming.base.StreamingSource` yields these and
nothing else, so a recording, a replay and a live feed look the same to the
bar builder, the runner and (from 21.2) the event engine.

- :class:`TradeTick`: one trade (price and size).
- :class:`QuoteTick`: the best bid and ask, and the last trade when the feed
  only sends snapshots (IBKR).
- :class:`StreamBar`: a finished bar a feed delivered itself.
- :class:`Heartbeat`: no data arrived for a while. It carries the time, so
  consumers can close bars and notice a silent stream.

Timestamps are timezone-aware UTC (a naive one is refused, since its zone
is a guess). Tickers use the lake's ids (``AAPL.US``), never the vendor's.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import ClassVar, Literal

from stonks.core.interval import Interval

EventKind = Literal["trade", "quote", "bar", "heartbeat"]

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _utc(when: datetime, what: str) -> datetime:
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError(f"{what} needs a timezone (got a naive datetime)")
    return when.astimezone(UTC)


def _ticker(ticker: str) -> None:
    if not ticker:
        raise ValueError("a stream event needs a ticker")


def _positive(value: float, what: str) -> None:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{what} must be a positive finite number, got {value!r}")


def _optional_price(value: float | None, what: str) -> None:
    if value is not None and not math.isfinite(value):
        raise ValueError(f"{what} must be finite when set, got {value!r}")


@dataclass(frozen=True, slots=True)
class TradeTick:
    ticker: str
    timestamp: datetime
    price: float
    size: float = 0.0
    source: str = ""

    kind: ClassVar[EventKind] = "trade"

    def __post_init__(self) -> None:
        _ticker(self.ticker)
        object.__setattr__(self, "timestamp", _utc(self.timestamp, "timestamp"))
        _positive(self.price, "price")
        if not math.isfinite(self.size) or self.size < 0:
            raise ValueError(f"size cannot be negative, got {self.size!r}")


@dataclass(frozen=True, slots=True)
class QuoteTick:
    ticker: str
    timestamp: datetime
    bid: float | None
    ask: float | None
    bid_size: float | None = None
    ask_size: float | None = None
    #: The last trade, for snapshot feeds that send it with the quote.
    last: float | None = None
    #: A delayed feed (no market data subscription). Never treated as live.
    delayed: bool = False
    source: str = ""

    kind: ClassVar[EventKind] = "quote"

    def __post_init__(self) -> None:
        _ticker(self.ticker)
        object.__setattr__(self, "timestamp", _utc(self.timestamp, "timestamp"))
        _optional_price(self.bid, "bid")
        _optional_price(self.ask, "ask")
        _optional_price(self.last, "last")

    @property
    def mid(self) -> float | None:
        """The mid of a sound quote: both sides positive and not crossed."""
        bid, ask = self.bid, self.ask
        if bid is None or ask is None or bid <= 0 or ask <= 0 or bid > ask:
            return None
        return (bid + ask) / 2.0

    @property
    def reference(self) -> float | None:
        """The last trade when positive, else the mid."""
        if self.last is not None and self.last > 0:
            return self.last
        return self.mid


@dataclass(frozen=True, slots=True)
class StreamBar:
    """A finished bar. ``timestamp`` is the bar's start, as in ``bars``."""

    ticker: str
    timestamp: datetime
    interval: Interval
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    source: str = ""

    kind: ClassVar[EventKind] = "bar"

    def __post_init__(self) -> None:
        _ticker(self.ticker)
        object.__setattr__(self, "timestamp", _utc(self.timestamp, "timestamp"))
        if not self.interval.is_intraday:
            raise ValueError(f"a stream bar is intraday, got {self.interval.code}")
        for name in ("open", "high", "low", "close"):
            _positive(float(getattr(self, name)), name)
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high is below open, close or low")
        if self.low > min(self.open, self.close):
            raise ValueError("low is above open or close")
        if self.volume < 0:
            raise ValueError(f"volume cannot be negative, got {self.volume}")

    @property
    def adj_close(self) -> float:
        """Intraday bars carry no adjustment: ``adj_close`` is ``close``,
        as in the REST intraday ingest."""
        return self.close


@dataclass(frozen=True, slots=True)
class Heartbeat:
    timestamp: datetime
    source: str = ""

    kind: ClassVar[EventKind] = "heartbeat"

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", _utc(self.timestamp, "timestamp"))


StreamEvent = TradeTick | QuoteTick | StreamBar | Heartbeat
DataEvent = TradeTick | QuoteTick | StreamBar


def event_ticker(event: StreamEvent) -> str | None:
    """The event's ticker, ``None`` for a heartbeat."""
    return None if isinstance(event, Heartbeat) else event.ticker


def bucket_start(when: datetime, interval: Interval) -> datetime:
    """The start of the ``interval`` bar holding ``when`` (UTC, aligned to
    the epoch, so a 5m bar starts at :00, :05 and so on)."""
    if not interval.is_intraday:
        raise ValueError(f"bucket_start takes an intraday interval, got {interval.code}")
    at = _utc(when, "when")
    micros = (at - _EPOCH) // timedelta(microseconds=1)
    step = interval.seconds * 1_000_000
    return _EPOCH + timedelta(microseconds=micros - micros % step)
