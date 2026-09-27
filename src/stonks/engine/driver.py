"""The event driver: one loop for replay, the backtest and live (roadmap 21.2.1).

:class:`EventDriver` pulls events from any
:class:`~stonks.streaming.base.StreamingSource` (a live feed, a recording
through ``replay``, lake bars through ``lake_bars``) and hands every bar
close to the registered handlers.

- **Bars.** Every event goes through a
  :class:`~stonks.streaming.bars.BarBuilder`, so ticks become bars and a
  bar a source delivered itself passes through. Bars of another interval
  are ignored.
- **Bar close events.** All bars that close at the same moment (start plus
  interval) make one :class:`BarClose`, sorted by ticker. It is dispatched
  once its settle time (close plus ``settle``, the builder's grace by
  default) has passed, so a slow ticker's bar for the same minute is still
  in it. A bar that closes at or before a moment already dispatched is late:
  it is dropped and counted, so nothing is decided twice.
- **Time.** With a :class:`~stonks.core.clock.FakeClock` (replay and the
  backtest) the driver owns the clock. Event time moves it forward, never
  back, and during a dispatch it reads the event's settle time, the same
  moment a live run decides at. Any other clock is live: the driver only
  reads it.
- **Handlers.** A handler is an object with ``on_bar_close(event)`` or a
  plain callable. They run in ``priority`` order (lower first), then in
  registration order. Gates such as session rules use a negative priority.
  A handler that raises is logged and counted per handler, and the others
  still run.
- **End of a run.** A finite source (a replay) closes and dispatches every
  bar at its end. A live stream that ends or is stopped dispatches what has
  closed and keeps the bar in progress. A disconnect propagates and keeps
  every pending close, so the next :meth:`EventDriver.run` carries on.

:meth:`EventDriver.on_event` is public, so the driver can also sit behind
the :class:`~stonks.streaming.runner.StreamRunner` as a subscriber.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from stonks.core.clock import SYSTEM_CLOCK, Clock, FakeClock
from stonks.core.interval import Interval
from stonks.core.stream import Heartbeat, StreamBar, StreamEvent
from stonks.logging import get_logger
from stonks.streaming.bars import BarBuilder
from stonks.streaming.base import StreamingSource

_log = get_logger("stonks.engine.driver")


@dataclass(frozen=True, slots=True)
class BarClose:
    """Every bar that closed at ``at``, sorted by ticker."""

    #: The close shared by every bar (bar start plus the interval).
    at: datetime
    interval: Interval
    bars: tuple[StreamBar, ...]
    #: 1, 2, 3, ... per driver: the dispatch order.
    sequence: int

    @property
    def tickers(self) -> tuple[str, ...]:
        return tuple(b.ticker for b in self.bars)

    def bar(self, ticker: str) -> StreamBar | None:
        return next((b for b in self.bars if b.ticker == ticker), None)


@runtime_checkable
class BarCloseHandler(Protocol):
    def on_bar_close(self, event: BarClose) -> object:
        """React to a bar close. The clock reads the decision time."""
        ...


Handler = BarCloseHandler | Callable[[BarClose], object]


@dataclass
class DriverStats:
    events: int = 0
    bars: int = 0
    bar_closes: int = 0
    late_bars: int = 0
    handler_errors: dict[str, int] = field(default_factory=dict)
    last_close_at: datetime | None = None

    @property
    def total_handler_errors(self) -> int:
        return sum(self.handler_errors.values())


@dataclass(frozen=True)
class _Registered:
    name: str
    priority: int
    order: int
    call: Callable[[BarClose], object]


def _handler_name(handler: Handler) -> str:
    name = getattr(handler, "name", None)
    if isinstance(name, str) and name:
        return name
    target = handler if not isinstance(handler, BarCloseHandler) else type(handler)
    return getattr(target, "__qualname__", type(handler).__name__)


class EventDriver:
    def __init__(
        self,
        source: StreamingSource,
        *,
        clock: Clock = SYSTEM_CLOCK,
        interval: Interval = Interval.MIN_1,
        builder: BarBuilder | None = None,
        settle: timedelta | None = None,
    ) -> None:
        self.source = source
        self.clock = clock
        self.builder = builder or BarBuilder(interval=interval)
        self.interval = self.builder.interval
        self.settle = self.builder.grace if settle is None else settle
        if self.settle < timedelta(0):
            raise ValueError("settle cannot be negative")
        self.stats = DriverStats()
        self._step = self.interval.to_timedelta()
        self._handlers: list[_Registered] = []
        self._pending: dict[datetime, dict[str, StreamBar]] = {}
        self._dispatched_through: datetime | None = None
        self._now: datetime | None = None
        self._sequence = 0
        self._stop = threading.Event()

    @property
    def replay(self) -> bool:
        """Whether the driver owns the clock (replay and the backtest)."""
        return isinstance(self.clock, FakeClock)

    @property
    def pending_closes(self) -> int:
        return len(self._pending)

    # ---- handlers --------------------------------------------------------------------

    def register(self, handler: Handler, *, name: str | None = None, priority: int = 0) -> str:
        """Add a bar close handler. Returns its name (unique per driver)."""
        label = name or _handler_name(handler)
        if any(h.name == label for h in self._handlers):
            raise ValueError(f"a handler named {label!r} is already registered")
        call = handler.on_bar_close if isinstance(handler, BarCloseHandler) else handler
        self._handlers.append(_Registered(label, priority, len(self._handlers), call))
        self._handlers.sort(key=lambda h: (h.priority, h.order))
        return label

    def handler_names(self) -> list[str]:
        """Handler names in dispatch order."""
        return [h.name for h in self._handlers]

    # ---- the loop --------------------------------------------------------------------

    def run(self, tickers: Sequence[str]) -> DriverStats:
        """Pull the source until it ends or :meth:`stop` is called."""
        self._stop.clear()
        for event in self.source.stream(tickers):
            self.on_event(event)
            if self._stop.is_set():
                break
        self.finish(drain=self.source.finite and not self._stop.is_set())
        return self.stats

    def stop(self) -> None:
        """End :meth:`run` soon. Safe from another thread or a handler."""
        self._stop.set()
        self.source.close()

    def on_event(self, event: StreamEvent) -> list[BarClose]:
        """Feed one event. Returns the bar closes it dispatched."""
        self.stats.events += 1
        when = self._event_time(event)
        now = self._flush_time(when)
        bars = self.builder.on_event(event)
        if not isinstance(event, Heartbeat):
            bars += self.builder.close_due(now)
        self._collect(bars)
        done = self._dispatch_due(now)
        self._advance(when)
        return done

    def finish(self, *, drain: bool) -> list[BarClose]:
        """Dispatch every pending close. ``drain`` also closes the bars in
        progress (the end of a replay). Without it, only bars whose minute
        has ended are closed."""
        now = self._flush_time(None)
        self._collect(self.builder.drain() if drain else self.builder.flush(now))
        return [self._dispatch(at) for at in sorted(self._pending)]

    # ---- internals -------------------------------------------------------------------

    def _event_time(self, event: StreamEvent) -> datetime:
        if isinstance(event, StreamBar):
            return event.timestamp + event.interval.to_timedelta()
        return event.timestamp

    def _advance(self, when: datetime) -> None:
        """Move replay time forward to ``when``, never back."""
        if not self.replay:
            return
        if self._now is None or when > self._now:
            self._now = when
        self._set_clock(self._now)

    def _set_clock(self, when: datetime) -> None:
        if isinstance(self.clock, FakeClock):
            self.clock.set(when)

    def _flush_time(self, when: datetime | None) -> datetime:
        """The time pending closes are measured against: event time in
        replay (never behind the replay clock), the clock live."""
        if not self.replay:
            return self.clock.now()
        if when is None or (self._now is not None and self._now > when):
            return self._now if self._now is not None else self.clock.now()
        return when

    def _collect(self, bars: list[StreamBar]) -> None:
        for bar in bars:
            close = bar.timestamp + self._step
            if self._dispatched_through is not None and close <= self._dispatched_through:
                self.stats.late_bars += 1
                _log.warning("engine.late_bar", ticker=bar.ticker, close=close.isoformat())
                continue
            self.stats.bars += 1
            self._pending.setdefault(close, {})[bar.ticker] = bar

    def _dispatch_due(self, now: datetime) -> list[BarClose]:
        due = sorted(at for at in self._pending if at + self.settle <= now)
        return [self._dispatch(at) for at in due]

    def _dispatch(self, at: datetime) -> BarClose:
        group = self._pending.pop(at)
        self._sequence += 1
        event = BarClose(
            at=at,
            interval=self.interval,
            bars=tuple(group[t] for t in sorted(group)),
            sequence=self._sequence,
        )
        self._dispatched_through = at
        self.stats.bar_closes += 1
        self.stats.last_close_at = at
        if self.replay:
            # decide at the settle time, the moment a live run decides at
            decide = at + self.settle
            if self._now is not None and self._now > decide:
                decide = self._now
            self._now = decide
            self._set_clock(decide)
        for handler in self._handlers:
            self._call(handler, event)
        return event

    def _call(self, handler: _Registered, event: BarClose) -> None:
        try:
            handler.call(event)
        except Exception as exc:
            errors = self.stats.handler_errors
            errors[handler.name] = errors.get(handler.name, 0) + 1
            _log.warning(
                "engine.handler_failed",
                handler=handler.name,
                close=event.at.isoformat(),
                error=str(exc),
            )
