"""The ``ibkr`` streaming source: live prices through the IBKR adapter
(roadmap 21.1).

It polls the broker's :class:`~stonks.execution.brokers.base.QuoteSource`
capability (market data snapshots) every ``[streaming.ibkr] poll_seconds``
and yields a :class:`~stonks.core.stream.QuoteTick` for each quote that
changed, then a heartbeat. There is no new vendor code: contracts, errors,
pacing and the delayed-data flag all come from ``execution/brokers/ibkr/``.
It connects under its own read-only API client id (``stream``, 15), so it
never competes with the tick's session.

Snapshots carry no trade sizes, so bars built from them have volume 0 and
see only the prices at each poll. The EODHD trade feed is the better bar
source. This one is the broker's own view of the price, for the price band
and the live checks of 21.2 and 21.3.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from typing import Self

from stonks.core.clock import SYSTEM_CLOCK, Clock
from stonks.core.stream import Heartbeat, QuoteTick, StreamEvent
from stonks.execution.brokers.base import BrokerError, LiveTradingRefusedError, QuoteSource
from stonks.execution.brokers.ibkr.factory import connect_ibkr
from stonks.streaming.base import (
    StreamAuthError,
    StreamContext,
    StreamDisconnectedError,
    StreamingSource,
)
from stonks.streaming.registry import register_stream_source

SOURCE = "ibkr"


def _utc(when: datetime) -> datetime:
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when


@register_stream_source("ibkr")
class IbkrQuoteStream(StreamingSource):
    def __init__(
        self,
        quotes: QuoteSource,
        *,
        poll_seconds: float = 5.0,
        clock: Clock = SYSTEM_CLOCK,
        sleep: Callable[[float], object] | None = None,
    ) -> None:
        self._quotes = quotes
        self.poll_seconds = poll_seconds
        self.clock = clock
        self._stop = threading.Event()
        self._sleep: Callable[[float], object] = sleep or self._stop.wait

    @classmethod
    def from_settings(cls, ctx: StreamContext) -> Self:
        cfg = ctx.settings.streaming.ibkr
        broker = connect_ibkr(
            ctx.settings.brokers.ibkr,
            gateway=cfg.gateway,
            role="stream",
            state=ctx.state,
            clock=ctx.clock,
        )
        return cls(broker, poll_seconds=cfg.poll_seconds, clock=ctx.clock)

    def _poll(self, tickers: Sequence[str]) -> dict[str, QuoteTick]:
        try:
            answer = self._quotes.quotes(tickers)
        except LiveTradingRefusedError as exc:
            raise StreamAuthError(f"IBKR refused the session: {exc}") from exc
        except (BrokerError, ConnectionError, TimeoutError) as exc:
            raise StreamDisconnectedError(
                f"IBKR quotes failed: {exc or type(exc).__name__}"
            ) from exc
        return {
            ticker: QuoteTick(
                ticker,
                _utc(q.as_of),
                bid=q.bid,
                ask=q.ask,
                last=q.last,
                delayed=q.delayed,
                source=SOURCE,
            )
            for ticker, q in answer.items()
        }

    def stream(self, tickers: Sequence[str]) -> Iterator[StreamEvent]:
        self._stop.clear()
        seen: dict[str, tuple[object, ...]] = {}
        wanted = list(tickers)
        while not self._stop.is_set():
            for ticker, tick in sorted(self._poll(wanted).items()):
                key = (tick.timestamp, tick.last, tick.bid, tick.ask, tick.delayed)
                if seen.get(ticker) == key:
                    continue
                seen[ticker] = key
                yield tick
            if self._stop.is_set():
                return
            self._sleep(self.poll_seconds)
            yield Heartbeat(self.clock.now(), SOURCE)

    def close(self) -> None:
        self._stop.set()
