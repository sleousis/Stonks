"""AppContext — how every transport opens the lake, state, registry and data
source from ``Settings``.

Connections are opened per operation (per request, per job) rather than
shared: a DuckDB or sqlite3 connection must stay on the thread that uses it,
and transports run operations on worker threads. While the context is
started it keeps one anchor lake connection open so DuckDB's in-process
database instance stays warm and per-operation connects are cheap.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager

from stonks.app.errors import ConfigurationError
from stonks.config import Settings
from stonks.ingest.sources.base import DataSource
from stonks.logging import get_logger
from stonks.registry.store import StrategyRegistry
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState

_log = get_logger("stonks.app.context")

SourceFactory = Callable[[], DataSource]


class AppContext:
    def __init__(self, settings: Settings, *, source_factory: SourceFactory | None = None) -> None:
        self.settings = settings
        self._source_factory = source_factory
        self._anchor: DuckDBLake | None = None

    # ---- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Apply pending migrations to both stores and pin the lake open."""
        if self._anchor is not None:
            return
        anchor = DuckDBLake(self.settings.lake.path)
        anchor.migrate()
        with SqliteState(self.settings.state.path) as state:
            state.migrate()
        self._anchor = anchor
        _log.info("app.context.started")

    def close(self) -> None:
        if self._anchor is not None:
            self._anchor.close()
            self._anchor = None

    # ---- per-operation resources -------------------------------------------

    @contextmanager
    def lake(self) -> Iterator[DuckDBLake]:
        lake = DuckDBLake(self.settings.lake.path)
        try:
            yield lake
        finally:
            lake.close()

    @contextmanager
    def state(self) -> Iterator[SqliteState]:
        state = SqliteState(self.settings.state.path)
        try:
            yield state
        finally:
            state.close()

    @contextmanager
    def registry(self) -> Iterator[StrategyRegistry]:
        with self.state() as state:
            yield self.registry_on(state)

    def registry_on(self, state: SqliteState) -> StrategyRegistry:
        return StrategyRegistry(state=state, artifacts_dir=self.settings.registry.artifacts_dir)

    def build_source(self) -> DataSource:
        """The configured data source; raises ``ConfigurationError`` when
        it can't be built (e.g. no EODHD key)."""
        if self._source_factory is not None:
            return self._source_factory()
        eodhd = self.settings.sources.eodhd
        if not eodhd.api_key:
            raise ConfigurationError("EODHD_API_KEY is not set; ingest is unavailable")
        from stonks.ingest.sources.eodhd import EodhdDataSource

        return EodhdDataSource(
            api_key=eodhd.api_key,
            base_url=eodhd.base_url,
            timeout_seconds=eodhd.timeout_seconds,
            max_retries=eodhd.max_retries,
            retry_backoff_seconds=eodhd.retry_backoff_seconds,
        )
