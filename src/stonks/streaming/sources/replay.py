"""The ``replay`` streaming source (roadmap 21.1).

Plays a recording (:mod:`stonks.streaming.recorder`) back through the
:class:`~stonks.streaming.base.StreamingSource` interface. Hermetic tests
use it instead of a vendor, and the event engine of 21.2 uses it as its
replay driver.

- ``speed=None`` yields as fast as the consumer reads. ``speed=1.0`` waits
  the recorded gaps, ``10.0`` a tenth of them.
- With a :class:`~stonks.core.clock.FakeClock`, the clock is set to each
  event's time before the event is yielded, so everything downstream reads
  event time, not wall time.
- The source is finite: the runner stops when the recording ends.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Self

from stonks.core.clock import FakeClock
from stonks.core.stream import StreamEvent
from stonks.streaming.base import StreamContext, StreamingSource
from stonks.streaming.recorder import read_recording
from stonks.streaming.registry import register_stream_source


@register_stream_source("replay")
class ReplaySource(StreamingSource):
    finite = True

    def __init__(
        self,
        path: str | Path,
        *,
        speed: float | None = None,
        clock: FakeClock | None = None,
        sleep: Callable[[float], object] | None = None,
    ) -> None:
        if speed is not None and speed <= 0:
            raise ValueError("replay speed must be positive")
        self.path = Path(path)
        self.speed = speed
        self.clock = clock
        self._stop = threading.Event()
        self._sleep: Callable[[float], object] = sleep or self._stop.wait

    @classmethod
    def from_settings(cls, ctx: StreamContext) -> Self:
        cfg = ctx.settings.streaming.replay
        clock = ctx.clock if isinstance(ctx.clock, FakeClock) else None
        return cls(cfg.path, speed=cfg.speed, clock=clock)

    def stream(self, tickers: Sequence[str]) -> Iterator[StreamEvent]:
        self._stop.clear()
        previous = None
        for event in read_recording(self.path, tickers=list(tickers) or None):
            if self._stop.is_set():
                return
            if self.speed is not None and previous is not None:
                gap = (event.timestamp - previous).total_seconds() / self.speed
                if gap > 0:
                    self._sleep(gap)
                    if self._stop.is_set():
                        return
            previous = event.timestamp
            if self.clock is not None:
                self.clock.set(event.timestamp)
            yield event

    def close(self) -> None:
        self._stop.set()
