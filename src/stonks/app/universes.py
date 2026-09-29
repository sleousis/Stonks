"""UniverseService: stored universes (roadmap 10.5) for every transport.

Definitions are created, listed, shown and deleted directly. A refresh
(materialise the definition into ``universe_membership``) and an ensure
(fetch the members' missing bars) write the lake and may reach a vendor,
so they run as background jobs on the ``lake_write`` lane: DuckDB takes
one writer, and ``stonks serve`` holds it.
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, model_validator

from stonks.app.context import AppContext
from stonks.app.errors import ConflictError, NotFoundError, ValidationError
from stonks.app.ingest import SourceId
from stonks.app.jobs import Job, JobContext, JobRunner
from stonks.app.pagination import Page
from stonks.core.clock import SYSTEM_CLOCK, today
from stonks.core.interval import Interval
from stonks.ingest.ensure import DataEnsurer, EnsureReport, EnsureSettings
from stonks.ingest.wiring import build_ingest_pipeline
from stonks.logging import get_logger
from stonks.universes import (
    UniverseDefinition,
    UniverseKind,
    UniverseStore,
    refresh_universe,
)
from stonks.universes.base import EARLIEST, UNIVERSE_ID_PATTERN
from stonks.universes.index_import import parse_index_history
from stonks.universes.providers.static_list import parse_list_csv

UNIVERSE_REFRESH_JOB = "universe_refresh"
UNIVERSE_ENSURE_JOB = "universe_ensure"

_log = get_logger("stonks.app.universes")


class UniverseView(BaseModel):
    id: str
    kind: UniverseKind
    name: str | None = None
    description: str | None = None
    spec: dict[str, Any]
    created_at: datetime | None = None
    updated_at: datetime | None = None
    #: Last refresh; ``None`` until the first one (no members yet).
    refreshed_at: datetime | None = None
    #: Distinct tickers over the whole history at the last refresh.
    member_count: int | None = None


class UniverseUpdate(BaseModel):
    """A universe's new definition. The members stay as they are until the
    next refresh."""

    kind: UniverseKind
    name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    #: Kind-specific settings (see docs/universes.md).
    spec: dict[str, Any] = Field(default_factory=dict)
    #: ``list`` only: CSV text with a ``ticker`` column and optional
    #: ``start_date`` / ``end_date`` columns; replaces ``spec``.
    csv: str | None = Field(default=None, max_length=5_000_000)

    @model_validator(mode="after")
    def _csv_is_for_lists(self) -> Self:
        if self.csv is not None and self.kind != "list":
            raise ValueError("csv is only for list universes")
        return self


class UniverseCreate(UniverseUpdate):
    id: str = Field(pattern=UNIVERSE_ID_PATTERN)


class MembershipSpanView(BaseModel):
    """One stretch of membership: a member from ``start_date`` up to the
    day before ``end_date``."""

    ticker: str
    #: ``None``: a member from the start.
    start_date: date | None = None
    #: ``None``: still a member.
    end_date: date | None = None


class ExchangeView(BaseModel):
    """An exchange our instruments name, for the exchange picker."""

    exchange: str
    instruments: int
    #: Instruments not marked delisted.
    listed: int


class UniverseMembers(BaseModel):
    universe_id: str
    as_of: date
    tickers: list[str]
    count: int


class UniverseRefreshView(BaseModel):
    universe_id: str
    kind: str
    members: int
    current_members: int
    spans: int
    refreshed_at: datetime | None = None
    warnings: list[str] = Field(default_factory=list)


class EnsureDataRequest(BaseModel):
    """Fetch the missing bars of the universe's members over a window
    (every name that was a member on any day of it, delisted ones too)."""

    start: date
    end: date
    interval: str = "1d"
    #: Data source (default: the configured default source).
    source: SourceId | None = None

    @model_validator(mode="after")
    def _window(self) -> Self:
        if self.start > self.end:
            raise ValueError("start must be on or before end")
        return self


class IndexHistoryImport(BaseModel):
    """An index constituent history as CSV (``date,ticker,action`` with
    ``add``, ``remove`` or ``member``) or JSON (``as_of``,
    ``constituents``, ``changes``)."""

    index_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_]{0,63}$")
    format: Literal["csv", "json"] = "csv"
    content: str = Field(min_length=1, max_length=20_000_000)


class IndexHistoryView(BaseModel):
    index_id: str
    as_of: date | None
    constituents: int
    changes: int


def _view(d: UniverseDefinition) -> UniverseView:
    return UniverseView(**d.model_dump())


def _definition_of(universe_id: str, request: UniverseUpdate) -> UniverseDefinition:
    """The validated definition a create or update asks for (422 when not)."""
    spec = request.spec
    try:
        if request.csv is not None:
            spec = parse_list_csv(request.csv)
        definition = UniverseDefinition(
            id=universe_id,
            kind=request.kind,
            name=request.name,
            description=request.description,
            spec=spec,
        )
        definition.validated()
    except ValueError as exc:
        raise ValidationError(str(exc)) from None
    return definition


#: Lake writers share one lane so DuckDB never sees two writers on the same
#: rows (see :mod:`stonks.app.jobs`). Short request writes join it too.
LAKE_WRITE_LANE = "lake_write"


class UniverseService:
    def __init__(self, context: AppContext, runner: JobRunner) -> None:
        self._ctx = context
        self._runner = runner
        # Research work on the lake lane: whoever queued it may cancel it.
        for kind, handler in (
            (UNIVERSE_REFRESH_JOB, self._handle_refresh),
            (UNIVERSE_ENSURE_JOB, self._handle_ensure),
        ):
            runner.register(kind, handler, lock=LAKE_WRITE_LANE, operation=False)

    # ---- definitions -------------------------------------------------------------

    def list(self) -> list[UniverseView]:
        with self._ctx.lake() as lake:
            return [_view(d) for d in UniverseStore(lake).list()]

    def get(self, universe_id: str) -> UniverseView:
        with self._ctx.lake() as lake:
            return _view(self._definition(UniverseStore(lake), universe_id))

    def create(self, request: UniverseCreate) -> UniverseView:
        definition = _definition_of(request.id, request)

        def write() -> UniverseDefinition:
            with self._ctx.lake() as lake:
                store = UniverseStore(lake)
                if store.exists(request.id):
                    raise ConflictError(f"universe {request.id!r} already exists")
                return store.save(definition)

        saved = self._runner.run_in_lane(LAKE_WRITE_LANE, write)
        _log.info("universe.created", universe_id=saved.id, kind=saved.kind)
        return _view(saved)

    def update(self, universe_id: str, request: UniverseUpdate) -> UniverseView:
        """Replace the definition. The refresh fields stay, so the members
        read as before until the next refresh."""
        definition = _definition_of(universe_id, request)

        def write() -> UniverseDefinition:
            with self._ctx.lake() as lake:
                store = UniverseStore(lake)
                self._definition(store, universe_id)
                return store.save(definition)

        saved = self._runner.run_in_lane(LAKE_WRITE_LANE, write)
        _log.info("universe.updated", universe_id=saved.id, kind=saved.kind)
        return _view(saved)

    def history(
        self, universe_id: str, *, ticker: str | None, limit: int, offset: int
    ) -> Page[MembershipSpanView]:
        """The universe's membership spans, latest change first."""
        with self._ctx.lake() as lake:
            store = UniverseStore(lake)
            self._definition(store, universe_id)
            spans, total = store.membership_history(
                universe_id, ticker=ticker, limit=limit, offset=offset
            )
        items = [
            MembershipSpanView(
                ticker=s.ticker,
                start_date=None if s.start_date <= EARLIEST else s.start_date,
                end_date=s.end_date,
            )
            for s in spans
        ]
        return Page[MembershipSpanView](items=items, total=total, limit=limit, offset=offset)

    def exchanges(self) -> list[ExchangeView]:
        """Every exchange the lake's instruments name, with counts."""
        with self._ctx.lake() as lake:
            rows = UniverseStore(lake).exchanges()
        return [ExchangeView(exchange=e, instruments=n, listed=listed) for e, n, listed in rows]

    def delete(self, universe_id: str) -> UniverseView:
        """Delete the definition and its members. 409 while a refresh or
        ensure job for it is queued or running, so it can't write the
        members back afterwards."""
        busy = [
            job.id
            for job in self._runner.store.pending((UNIVERSE_REFRESH_JOB, UNIVERSE_ENSURE_JOB))
            if job.params.get("universe_id") == universe_id
        ]
        if busy:
            raise ConflictError(
                f"universe {universe_id!r} has pending jobs ({', '.join(busy)}); "
                "wait for them or cancel them first"
            )

        def write() -> UniverseDefinition:
            with self._ctx.lake() as lake:
                store = UniverseStore(lake)
                definition = self._definition(store, universe_id)
                store.delete(universe_id)
                return definition

        definition = self._runner.run_in_lane(LAKE_WRITE_LANE, write)
        _log.info("universe.deleted", universe_id=universe_id)
        return _view(definition)

    def members(self, universe_id: str, as_of: date | None = None) -> UniverseMembers:
        day = as_of or today(SYSTEM_CLOCK)  # the UTC date, like every stored timestamp
        with self._ctx.lake() as lake:
            self._definition(UniverseStore(lake), universe_id)
            tickers = lake.members_as_of(universe_id, day)
        return UniverseMembers(
            universe_id=universe_id, as_of=day, tickers=tickers, count=len(tickers)
        )

    def import_index_history(self, request: IndexHistoryImport) -> IndexHistoryView:
        try:
            history = parse_index_history(
                request.content, request.format, index_id=request.index_id
            )
        except ValueError as exc:
            raise ValidationError(str(exc)) from None

        def write() -> None:
            with self._ctx.lake() as lake:
                UniverseStore(lake).save_index_history(history)

        self._runner.run_in_lane(LAKE_WRITE_LANE, write)
        return IndexHistoryView(
            index_id=history.index_id,
            as_of=history.as_of,
            constituents=len(history.constituents),
            changes=len(history.changes),
        )

    # ---- refresh -------------------------------------------------------------------

    def submit_refresh(self, universe_id: str, *, owner_id: str | None = None) -> Job:
        self.get(universe_id)  # NotFoundError before queueing
        return self._runner.submit(
            UNIVERSE_REFRESH_JOB, {"universe_id": universe_id}, owner_id=owner_id
        )

    def refresh(self, universe_id: str) -> UniverseRefreshView:
        with self._ctx.lake() as lake:
            self._definition(UniverseStore(lake), universe_id)
            try:
                result = refresh_universe(
                    lake, universe_id, source_factory=lambda sid: self._ctx.build_source(sid)
                )
            except ValueError as exc:
                raise ValidationError(str(exc)) from None
        return UniverseRefreshView(**dataclasses.asdict(result))

    # ---- ensure data ---------------------------------------------------------------

    def submit_ensure(
        self, universe_id: str, request: EnsureDataRequest, *, owner_id: str | None = None
    ) -> Job:
        self.get(universe_id)
        _interval(request.interval)
        self._ctx.build_source(request.source)  # fail fast when it isn't configured
        body = {"universe_id": universe_id, **request.model_dump(mode="json")}
        return self._runner.submit(UNIVERSE_ENSURE_JOB, body, owner_id=owner_id)

    def ensure(
        self, universe_id: str, request: EnsureDataRequest, progress: JobContext | None = None
    ) -> EnsureReport:
        interval = _interval(request.interval)
        source = self._ctx.build_source(request.source)
        with self._ctx.lake() as lake:
            self._definition(UniverseStore(lake), universe_id)
            tickers = lake.members_between(universe_id, request.start, request.end)
            if progress is not None:
                progress.progress(
                    0.05, f"{len(tickers)} members between {request.start} and {request.end}"
                )
            settings = self._ctx.settings
            ensurer = DataEnsurer(
                lake,
                source,
                ensure_settings(settings),
                pipeline_factory=lambda src, lk: build_ingest_pipeline(settings, src, lk),
            )
            return ensurer.ensure(tickers, request.start, request.end, interval)

    # ---- helpers ----------------------------------------------------------------------

    @staticmethod
    def _definition(store: UniverseStore, universe_id: str) -> UniverseDefinition:
        try:
            return store.get(universe_id)
        except KeyError:
            raise NotFoundError(f"no universe {universe_id!r}") from None

    def _handle_refresh(self, params: dict[str, Any], ctx: JobContext) -> UniverseRefreshView:
        return self.refresh(params["universe_id"])

    def _handle_ensure(self, params: dict[str, Any], ctx: JobContext) -> EnsureReport:
        params = dict(params)
        universe_id = params.pop("universe_id")
        return self.ensure(universe_id, EnsureDataRequest.model_validate(params), progress=ctx)


def ensure_settings(settings: Any) -> EnsureSettings:
    """The ``[ensure]`` settings section when the app settings have one,
    else the defaults (the section lands with the config integration)."""
    section = getattr(settings, "ensure", None)
    if isinstance(section, EnsureSettings):
        return section
    if section is not None:
        return EnsureSettings.model_validate(
            section.model_dump() if hasattr(section, "model_dump") else section
        )
    return EnsureSettings()


def _interval(code: str) -> Interval:
    try:
        return Interval.parse(code)
    except (ValueError, TypeError) as exc:
        raise ValidationError(f"invalid interval {code!r}: {exc}") from None
