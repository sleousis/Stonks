"""AppContext — how every transport opens the lake, state, registry and data
source from ``Settings``.

Connections are opened per operation (per request, per job) rather than
shared: a DuckDB or sqlite3 connection must stay on the thread that uses it,
and transports run operations on worker threads. While the context is
started it keeps one anchor lake connection open so DuckDB's in-process
database instance stays warm and per-operation connects are cheap.

``settings`` is the TOML base with the admin's console overrides laid on
top (:mod:`stonks.config_overrides`). It is re-read at most every
:data:`OVERRIDES_TTL_SECONDS`, and at once after a change made through this
context, so blocks that read settings per run pick a change up without a
restart.
"""

from __future__ import annotations

import threading
import time
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

#: Longest time a process keeps using settings before it re-reads the
#: console overrides another process may have written.
OVERRIDES_TTL_SECONDS = 2.0


class AppContext:
    def __init__(self, settings: Settings, *, source_factory: SourceFactory | None = None) -> None:
        self._base = settings
        self._effective: Settings | None = None
        self._read_at = 0.0
        self._override_problems: dict[str, str] = {}
        self._lock = threading.Lock()
        self._source_factory = source_factory
        self._anchor: DuckDBLake | None = None

    # ---- settings ----------------------------------------------------------

    @property
    def settings(self) -> Settings:
        """The effective settings: the base with the console overrides."""
        with self._lock:
            now = time.monotonic()
            if self._effective is None or now - self._read_at >= OVERRIDES_TTL_SECONDS:
                self._effective = self._with_overrides()
                self._read_at = now
            return self._effective

    @settings.setter
    def settings(self, value: Settings) -> None:
        self._base = value
        self.invalidate_settings()

    @property
    def base_settings(self) -> Settings:
        """The TOML and env settings, without the console overrides."""
        return self._base

    @property
    def override_problems(self) -> dict[str, str]:
        """Stored overrides skipped because they no longer validate."""
        _ = self.settings
        return dict(self._override_problems)

    def invalidate_settings(self) -> None:
        """Re-read the overrides on the next access (after a change)."""
        with self._lock:
            self._effective = None

    def _with_overrides(self) -> Settings:
        from stonks.config_overrides import apply_overrides, load_override_values

        try:
            values = load_override_values(self._base.state.path)
        except Exception:  # a locked or broken state file never takes the app down
            _log.warning("app.context.overrides_unreadable", exc_info=True)
            return self._effective or self._base
        if not values:
            self._override_problems = {}
            return self._base
        effective, problems = apply_overrides(self._base, values)
        if problems and problems != self._override_problems:
            _log.warning("app.context.overrides_skipped", keys=sorted(problems))
        self._override_problems = problems
        return effective

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
