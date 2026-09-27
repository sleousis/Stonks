"""The ``StreamingSource`` seam (roadmap 21.1).

A streaming source pushes prices as they happen, where a
:class:`~stonks.ingest.sources.base.DataSource` answers requests. Each
source is one module in :mod:`stonks.streaming.sources` whose class is
decorated with :func:`~stonks.streaming.registry.register_stream_source`.

The contract:

- :meth:`StreamingSource.stream` connects, subscribes to ``tickers`` and
  yields :mod:`stonks.core.stream` events until the connection ends. It
  yields a :class:`~stonks.core.stream.Heartbeat` after
  ``[streaming] heartbeat_seconds`` without data, so a consumer always gets
  control back.
- A lost connection raises :class:`StreamDisconnectedError` (or ends the
  iterator). The runner reconnects with backoff. A refused login raises
  :class:`StreamAuthError`, which is never retried.
- :meth:`StreamingSource.close` may be called from another thread and makes
  a blocked :meth:`stream` return soon.
- A finite source (a replay) ends on its own. The runner then stops instead
  of reconnecting.

Vendor libraries are imported only inside their source module.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from stonks.core.clock import SYSTEM_CLOCK, Clock
from stonks.core.stream import StreamEvent

if TYPE_CHECKING:
    from stonks.config import Settings
    from stonks.store.state import SqliteState


class StreamError(RuntimeError):
    """A streaming source failed."""


class StreamDisconnectedError(StreamError):
    """The connection dropped or never opened. The runner retries."""


class StreamAuthError(StreamError):
    """The vendor refused the login. Retrying would not help."""


class StreamConfigError(StreamError, ValueError):
    """The source is unknown or missing configuration."""


@dataclass(frozen=True)
class StreamContext:
    """What a source may read to build itself."""

    settings: Settings
    clock: Clock = SYSTEM_CLOCK
    #: The state DB, for the IBKR contract cache. Optional.
    state: SqliteState | None = None


class StreamingSource(ABC):
    source_id: ClassVar[str] = ""
    #: A finite source ends on its own (a replay).
    finite: ClassVar[bool] = False

    @classmethod
    @abstractmethod
    def from_settings(cls, ctx: StreamContext) -> Self:
        """Build the source from ``[streaming]`` and the app settings."""

    @abstractmethod
    def stream(self, tickers: Sequence[str]) -> Iterator[StreamEvent]:
        """Connect, subscribe and yield events until the connection ends."""

    def close(self) -> None:  # noqa: B027  # optional hook, most sources need none
        """End a running :meth:`stream` soon. Safe from another thread."""

    def supports(self, ticker: str) -> bool:
        """Whether this source can stream ``ticker``."""
        del ticker
        return True
