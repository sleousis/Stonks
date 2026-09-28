"""ScreenerService: run screens, keep your saved ones, and turn a screen
into a stored universe for the lab (roadmap 20.8).

- Running a screen and reading saved screens need ``data.read``. A screen
  over ``[screener] max_candidates`` is a 422 that says how to narrow it.
  ``size`` counts the candidates first, and a big screen runs as the
  ``screen_run`` background job with progress (roadmap 20.11). Results are
  reused for a short time per spec and date. Saving,
  changing and deleting one need ``portfolio.manage`` (your own workspace,
  like watchlists). Another person's screen reads as missing (404).
- Saving a screen as a universe is research work (``lab.run``). The
  default ``rule`` mode stores a ``rule`` universe that runs the screen at
  each rebalance date, so the lab sees the names the screen picked on each
  day, dead ones too (P14). ``snapshot`` stores today's matches as a
  ``list``, which carries survivorship bias; the result says so.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from stonks.accounts.audit import iso_now
from stonks.app.context import AppContext
from stonks.app.errors import ConflictError, NotFoundError, ValidationError
from stonks.app.jobs import Job, JobContext, JobRunner
from stonks.app.universes import UniverseCreate, UniverseService, UniverseView
from stonks.auth.policy import Permission, require
from stonks.auth.principal import Principal
from stonks.logging import get_logger
from stonks.screener import (
    ScreenResult,
    ScreenSpec,
    TooManyCandidates,
    all_metrics,
    run_screen,
)
from stonks.screener.cache import ScreenCache
from stonks.screener.engine import Progress, candidates
from stonks.screener.metrics.base import MetricGroup, MetricUnit
from stonks.screener.settings import ScreenerSettings
from stonks.store.state import SqliteState
from stonks.universes.base import UNIVERSE_ID_PATTERN

#: Most saved screens one person may keep.
MAX_SCREENS = 100
#: A snapshot universe holds at most this many tickers.
MAX_SNAPSHOT = 5000
#: Default history of a rule universe made from a screen.
DEFAULT_RULE_DAYS = 365
#: The background job that runs a large screen (roadmap 20.11).
SCREEN_RUN_JOB = "screen_run"

_log = get_logger("stonks.app.screener")


class MetricView(BaseModel):
    id: str
    label: str
    group: MetricGroup
    unit: MetricUnit
    description: str


class ScreenRunRequest(BaseModel):
    """Run ``spec``, or one of your saved screens by ``screen_id``."""

    model_config = ConfigDict(extra="forbid")

    spec: ScreenSpec | None = None
    screen_id: str | None = Field(default=None, max_length=64)
    #: The day to screen on (default today).
    as_of: date | None = None

    @model_validator(mode="after")
    def _one(self) -> Self:
        if (self.spec is None) == (self.screen_id is None):
            raise ValueError("give either spec or screen_id")
        return self


class ScreenSize(BaseModel):
    """How big a screen is before any metric is read, and how to run it."""

    as_of: date
    #: Tickers the rule (and the universe) keep on the date.
    candidates: int
    max_candidates: int
    job_threshold: int
    #: More candidates than the cap: the screen would fail. Narrow it.
    over_cap: bool
    #: Big enough to run as a background job with progress.
    use_job: bool


def _name(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.strip()
    if not v:
        raise ValueError("name must not be blank")
    return v


class SavedScreenView(BaseModel):
    id: str
    name: str
    spec: ScreenSpec
    created_at: datetime
    updated_at: datetime


class SavedScreenCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    spec: ScreenSpec

    @field_validator("name")
    @classmethod
    def _clean(cls, v: str | None) -> str | None:
        return _name(v)


class SavedScreenUpdate(BaseModel):
    """Rename, replace the spec, or both. Unset fields stay."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=80)
    spec: ScreenSpec | None = None

    @field_validator("name")
    @classmethod
    def _clean(cls, v: str | None) -> str | None:
        return _name(v)


class ScreenUniverseRequest(BaseModel):
    """Store a screen (``spec`` or ``screen_id``) as a universe."""

    model_config = ConfigDict(extra="forbid")

    universe_id: str = Field(pattern=UNIVERSE_ID_PATTERN)
    name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    spec: ScreenSpec | None = None
    screen_id: str | None = Field(default=None, max_length=64)
    #: ``rule``: rerun the screen at each rebalance date (point in time).
    #: ``snapshot``: today's matches as a fixed list (survivorship bias).
    mode: Literal["rule", "snapshot"] = "rule"
    #: Rule mode: first rebalance date (default a year ago).
    start: date | None = None
    #: Rule mode: last date (default open, up to each refresh).
    end: date | None = None
    rebalance: Literal["weekly", "monthly", "quarterly"] = "monthly"
    #: Queue the refresh that fills the members (a background job).
    refresh: bool = True

    @model_validator(mode="after")
    def _check(self) -> Self:
        if (self.spec is None) == (self.screen_id is None):
            raise ValueError("give either spec or screen_id")
        if self.mode == "snapshot" and (self.start or self.end):
            raise ValueError("start and end are for rule mode")
        if self.start and self.end and self.start > self.end:
            raise ValueError("start must be on or before end")
        return self


class ScreenUniverseView(BaseModel):
    universe: UniverseView
    #: The refresh job that fills the members (``None`` when not asked).
    refresh_job: Job | None = None
    warnings: list[str] = Field(default_factory=list)


def _view(row: sqlite3.Row) -> SavedScreenView:
    r = dict(row)
    return SavedScreenView(
        id=r["id"],
        name=r["name"],
        spec=ScreenSpec.model_validate_json(r["spec_json"]),
        created_at=datetime.fromisoformat(r["created_at"]),
        updated_at=datetime.fromisoformat(r["updated_at"]),
    )


def _spec_json(spec: ScreenSpec) -> str:
    return json.dumps(spec.model_dump(mode="json", exclude_defaults=True), sort_keys=True)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ScreenerService:
    def __init__(
        self,
        context: AppContext,
        universes: UniverseService,
        runner: JobRunner | None = None,
        *,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._ctx = context
        self._universes = universes
        self._runner = runner
        self._clock = clock
        settings = self._settings()
        self._cache = ScreenCache(settings.cache_seconds, settings.cache_entries)
        if runner is not None:
            # a read-only research job: general lane, stoppable by its owner
            runner.register(SCREEN_RUN_JOB, self._handle_run, cancellable=True, operation=False)

    def _today(self) -> date:
        return self._clock().date()

    def _settings(self) -> ScreenerSettings:
        """``[screener]``, read on each call so a changed setting applies."""
        return getattr(self._ctx.settings, "screener", None) or ScreenerSettings()

    # ---- metrics and runs ------------------------------------------------------------

    @staticmethod
    def metrics() -> list[MetricView]:
        """Every metric a screen may filter or sort on."""
        return [
            MetricView(
                id=m.id, label=m.label, group=m.group, unit=m.unit, description=m.description
            )
            for m in all_metrics()
        ]

    def run(self, principal: Principal, request: ScreenRunRequest) -> ScreenResult:
        """Run a screen now. 422 over the candidate cap. A repeat of the same
        spec and date within ``[screener] cache_seconds`` is served from
        the cache."""
        require(principal, Permission.READ)
        spec, as_of = self._resolve(principal, request)
        return self._run(spec, as_of)

    def size(self, principal: Principal, request: ScreenRunRequest) -> ScreenSize:
        """Count the candidates (no metric is read) and say whether the
        screen is over the cap or big enough for the background job."""
        require(principal, Permission.READ)
        spec, as_of = self._resolve(principal, request)
        settings = self._settings()
        with self._ctx.lake() as lake:
            try:
                count = len(candidates(lake, spec, as_of))
            except KeyError as exc:
                raise NotFoundError(str(exc.args[0]) if exc.args else str(exc)) from None
        over = count > settings.max_candidates
        return ScreenSize(
            as_of=as_of,
            candidates=count,
            max_candidates=settings.max_candidates,
            job_threshold=settings.job_threshold,
            over_cap=over,
            use_job=not over and count > settings.job_threshold,
        )

    def submit(self, principal: Principal, request: ScreenRunRequest) -> Job:
        """Queue the screen as a background job with progress. The spec is
        fixed at submit time (a saved screen is read now, as its owner)."""
        require(principal, Permission.READ)
        if self._runner is None:
            raise ConflictError("background screens need the job runner")
        spec, as_of = self._resolve(principal, request)
        params = {"spec": spec.model_dump(mode="json"), "as_of": as_of.isoformat()}
        return self._runner.submit(SCREEN_RUN_JOB, params, owner_id=principal.user_id)

    def _resolve(self, principal: Principal, request: ScreenRunRequest) -> tuple[ScreenSpec, date]:
        spec = request.spec or self.get(principal, request.screen_id or "").spec
        return spec, request.as_of or self._today()

    def _handle_run(self, params: dict[str, Any], ctx: JobContext) -> ScreenResult:
        spec = ScreenSpec.model_validate(params["spec"])
        as_of = date.fromisoformat(params["as_of"])

        def progress(fraction: float, message: str) -> None:
            ctx.check_cancelled()
            ctx.progress(fraction, message)

        return self._run(spec, as_of, progress=progress)

    def _run(self, spec: ScreenSpec, as_of: date, progress: Progress | None = None) -> ScreenResult:
        hit = self._cache.get(spec, as_of)
        if hit is not None:
            _log.info("screener.cache_hit", as_of=as_of.isoformat())
            return hit.model_copy(update={"cached": True})
        with self._ctx.lake() as lake:
            try:
                result = run_screen(
                    lake,
                    spec,
                    as_of,
                    max_candidates=self._settings().max_candidates,
                    progress=progress,
                )
            except KeyError as exc:
                raise NotFoundError(str(exc.args[0]) if exc.args else str(exc)) from None
            except TooManyCandidates as exc:
                raise ValidationError(str(exc)) from None
        self._cache.put(spec, as_of, result)
        return result

    # ---- saved screens -----------------------------------------------------------------

    def list(self, principal: Principal) -> list[SavedScreenView]:
        """Your saved screens, oldest first."""
        require(principal, Permission.READ)
        with self._ctx.state() as state:
            rows = state.sql(
                "SELECT * FROM screens WHERE owner_id = ? ORDER BY created_at, id",
                [principal.user_id],
            )
        return [_view(r) for r in rows]

    def get(self, principal: Principal, screen_id: str) -> SavedScreenView:
        require(principal, Permission.READ)
        with self._ctx.state() as state:
            return _view(self._owned(state, principal, screen_id))

    def create(self, principal: Principal, body: SavedScreenCreate) -> SavedScreenView:
        require(principal, Permission.PORTFOLIO_MANAGE)
        now = iso_now()
        sid = f"scr_{secrets.token_hex(6)}"
        with self._ctx.state() as state, state.transaction():
            count = int(
                state.sql("SELECT COUNT(*) FROM screens WHERE owner_id = ?", [principal.user_id])[
                    0
                ][0]
            )
            if count >= MAX_SCREENS:
                raise ValidationError(f"you can keep at most {MAX_SCREENS} screens")
            self._check_name(state, principal, body.name, None)
            state.execute(
                "INSERT INTO screens (id, owner_id, name, spec_json, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [sid, principal.user_id, body.name, _spec_json(body.spec), now, now],
            )
            return _view(self._owned(state, principal, sid))

    def update(
        self, principal: Principal, screen_id: str, body: SavedScreenUpdate
    ) -> SavedScreenView:
        require(principal, Permission.PORTFOLIO_MANAGE)
        with self._ctx.state() as state, state.transaction():
            row = dict(self._owned(state, principal, screen_id))
            name = body.name if body.name is not None else row["name"]
            spec_json = _spec_json(body.spec) if body.spec is not None else row["spec_json"]
            if name != row["name"]:
                self._check_name(state, principal, name, screen_id)
            state.execute(
                "UPDATE screens SET name = ?, spec_json = ?, updated_at = ? WHERE id = ?",
                [name, spec_json, iso_now(), screen_id],
            )
            return _view(self._owned(state, principal, screen_id))

    def delete(self, principal: Principal, screen_id: str) -> None:
        require(principal, Permission.PORTFOLIO_MANAGE)
        with self._ctx.state() as state, state.transaction():
            self._owned(state, principal, screen_id)
            state.execute("DELETE FROM screens WHERE id = ?", [screen_id])

    # ---- save as a universe ------------------------------------------------------------

    def to_universe(
        self, principal: Principal, request: ScreenUniverseRequest
    ) -> ScreenUniverseView:
        require(principal, Permission.LAB_RUN)
        saved = self.get(principal, request.screen_id) if request.screen_id else None
        spec = request.spec or (saved.spec if saved else ScreenSpec())
        if spec.universe_id == request.universe_id:
            raise ValidationError("a screen universe cannot start from itself")
        warnings: list[str] = []
        description = request.description or (f"screen {saved.name!r}" if saved else None)
        if request.mode == "rule":
            start = request.start or self._today() - timedelta(days=DEFAULT_RULE_DAYS)
            body: dict[str, object] = spec.model_dump(mode="json", exclude_defaults=True)
            body |= {"start": start.isoformat(), "rebalance": request.rebalance}
            if request.end is not None:
                body["end"] = request.end.isoformat()
            create = UniverseCreate(
                id=request.universe_id,
                kind="rule",
                name=request.name,
                description=description,
                spec=body,
            )
        else:
            result = self._run(spec, self._today())
            tickers = [r.ticker for r in result.rows]
            if not tickers:
                raise ValidationError("the screen matched no ticker today")
            if len(tickers) > MAX_SNAPSHOT:
                raise ValidationError(f"a snapshot holds at most {MAX_SNAPSHOT} tickers")
            warnings.append(
                "a snapshot is today's matches as a fixed list: it carries survivorship "
                "bias (P14); rule mode reruns the screen at each rebalance date"
            )
            create = UniverseCreate(
                id=request.universe_id,
                kind="list",
                name=request.name,
                description=description,
                spec={"tickers": tickers},
            )
        universe = self._universes.create(create)
        job = (
            self._universes.submit_refresh(universe.id, owner_id=principal.user_id)
            if request.refresh
            else None
        )
        _log.info(
            "screener.universe_saved",
            universe_id=universe.id,
            mode=request.mode,
            screen_id=request.screen_id,
        )
        return ScreenUniverseView(universe=universe, refresh_job=job, warnings=warnings)

    # ---- helpers -------------------------------------------------------------------------

    @staticmethod
    def _owned(state: SqliteState, principal: Principal, screen_id: str) -> sqlite3.Row:
        rows = state.sql(
            "SELECT * FROM screens WHERE id = ? AND owner_id = ?", [screen_id, principal.user_id]
        )
        if not rows:
            raise NotFoundError(f"no screen with id {screen_id!r}")
        return rows[0]

    @staticmethod
    def _check_name(
        state: SqliteState, principal: Principal, name: str, except_id: str | None
    ) -> None:
        rows = state.sql(
            "SELECT id FROM screens WHERE owner_id = ? AND name = ? AND id != ?",
            [principal.user_id, name, except_id or ""],
        )
        if rows:
            raise ConflictError(f"you already have a screen named {name!r}")
