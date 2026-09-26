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

from stonks.app.errors import ConfigurationError, ValidationError
from stonks.config import Settings
from stonks.ingest.sources.base import DataSource
from stonks.ingest.sources.registry import (
    DEFAULT_SOURCE_ID,
    SOURCE_IDS,
    SourceConfigError,
    build_source,
)
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

    def build_source(self, source_id: str | None = None) -> DataSource:
        """The data source ``source_id`` (default: the registry's default)
        built from ``[sources]`` via :func:`stonks.ingest.sources.registry.build_source`.

        Raises ``ValidationError`` for an unknown id and
        ``ConfigurationError`` when it can't be built (e.g. no EODHD key).
        A ``source_factory`` given at construction (tests) wins for every id.
        """
        if self._source_factory is not None:
            return self._source_factory()
        sid = source_id or DEFAULT_SOURCE_ID
        if sid not in SOURCE_IDS:
            raise ValidationError(f"unknown source {sid!r}; choose one of {list(SOURCE_IDS)}")
        try:
            return build_source(sid, self.settings.sources)
        except SourceConfigError as exc:
            raise ConfigurationError(f"{sid} source unavailable: {exc}") from None
