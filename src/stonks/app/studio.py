"""StudioService — define, test and enable strategies from the UI.

A *draft* is a strategy being edited: a declarative rule spec
(``kind='rule'``, run by :class:`~stonks.strategies.rule_based.RuleStrategy`)
or, when explicitly enabled, Python source (``kind='code'``). Drafts can be
validated (spec validation plus a smoke run of ``estimate_return``),
backtested and lab-run as background jobs, registered (landing in
``shadow``, like every lab-registered strategy) and then enabled
(``active``) or disabled (back to ``shadow``) through the registry.

Code strategies — security model
--------------------------------
A code draft is **arbitrary Python executed inside the server process with
the server's privileges** (files, network, credentials in the
environment). There is no sandbox and no timeout: a smoke check cannot
interrupt a runaway loop. The feature is therefore off unless
``[api] allow_code_strategies = true``, which only makes sense for a
single trusted user on a trusted machine. The guard rails are:

- While disabled, every operation on a code draft raises
  :class:`CodeStrategiesDisabledError` (HTTP 403) *before* any file is
  written or imported; listing drafts shows code drafts without source.
  Queued code jobs re-check the flag when they start.
- Source is stored in the state DB and only written to disk when it must
  run, as ``<user_strategies_dir>/<draft id>_<sha256 prefix>.py``
  (content-addressed, so a registered strategy keeps pointing at the
  exact code it was registered with even if the draft is edited later).
- The module is imported from that path only, via
  ``importlib.util.spec_from_file_location``, under the
  ``stonks_user_strategies.`` namespace; the directory is never put on
  ``sys.path`` and nothing is imported at server start.
- A module must define exactly one :class:`BaseStrategy` subclass, whose
  ``parameter_spec`` defaults must validate and whose ``estimate_return``
  / ``decide`` must run on synthetic sample data, before it can be
  backtested, lab-run or registered.

Registered code strategies have class paths under
``stonks_user_strategies``; the directory is not importable, so a fresh
process resolves them only through the ``sys.meta_path`` hook in
:mod:`stonks.app.user_strategies`, which ``Services.start`` installs while
code strategies are enabled. Otherwise the Ranker skips them with a
warning.
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import math
import re
import sys
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from types import ModuleType
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from stonks.app.context import AppContext
from stonks.app.errors import AppError, ConflictError, NotFoundError, ValidationError
from stonks.app.jobs import Job, JobContext, JobRunner
from stonks.app.lab import (
    _OBJECTIVES,
    _SURVIVAL_TESTS,
    BacktestRequest,
    BacktestResult,
    EquityPoint,
    LabRunView,
    LabService,
    ObjectiveName,
    SurvivalTestName,
    TunerName,
)
from stonks.app.pagination import Page
from stonks.app.serialize import finite, to_jsonable
from stonks.app.strategies import StrategyRef, StrategyStatus, SurvivalReportView
from stonks.backtest.engine import BacktestConfig, Backtester
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.config import Settings
from stonks.core.interval import Interval
from stonks.core.params import validate_params
from stonks.core.protocols import Tuner
from stonks.core.types import Order, Portfolio
from stonks.lab.dataset import LabDataset
from stonks.lab.runner import LabRunner
from stonks.lab.survival.base import SurvivalSuite
from stonks.lab.tuning.grid import GridTuner
from stonks.lab.tuning.random import RandomTuner
from stonks.logging import get_logger
from stonks.strategies.base import BaseStrategy
from stonks.strategies.rule_based import RULE_STRATEGY_CLASS_PATH, RuleStrategy
from stonks.strategies.rules import TEMPLATES, RuleSpecError, rule_spec_json_schema, validate_spec
from stonks.strategies.rules.sample import SampleLake, synthetic_bars

STUDIO_BACKTEST_JOB = "studio_backtest"
STUDIO_LAB_RUN_JOB = "studio_lab_run"
USER_MODULE_PREFIX = "stonks_user_strategies"

DraftKind = Literal["rule", "code"]
DraftStatus = Literal["draft", "registered"]

_MAX_SOURCE_CHARS = 200_000
_SAMPLE_TICKERS = ("SAMPLE_A", "SAMPLE_B")
_SAMPLE_BARS = 400
_SAMPLE_EVAL_BARS = 20
_MODULE_NAME = re.compile(rf"^{USER_MODULE_PREFIX}\.[a-z0-9_]{{1,80}}$")

_log = get_logger("stonks.app.studio")


class CodeStrategiesDisabledError(AppError):
    """A code-draft operation while ``api.allow_code_strategies`` is off."""

    title = "Code strategies disabled"


def user_strategies_dir(settings: Settings) -> Path:
    """Where code drafts are written: ``user_strategies`` next to the
    registry's artifacts directory (``data/user_strategies`` by default)."""
    return Path(settings.registry.artifacts_dir).parent / "user_strategies"


class RuleStrategySource:
    """Catalog source offering :class:`RuleStrategy`, so a
    ``StrategyRef(class_path=RULE_STRATEGY_CLASS_PATH, params={"spec": …})``
    resolves through the catalog."""

    name = "studio"

    def discover(self) -> list[type]:
        return [RuleStrategy]


# ---- models -----------------------------------------------------------------


class RuleTemplateView(BaseModel):
    id: str
    title: str
    description: str
    spec: dict[str, Any]


class DraftCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: DraftKind = "rule"
    #: The RuleSpec for rule drafts (may be work in progress); the
    #: constructor params for code drafts.
    spec: dict[str, Any] = Field(default_factory=dict)
    source_code: str | None = Field(default=None, max_length=_MAX_SOURCE_CHARS)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value

    @model_validator(mode="after")
    def _kind_fields(self) -> Self:
        if self.kind == "rule" and self.source_code is not None:
            raise ValueError("source_code is only allowed on code drafts")
        if self.kind == "code" and not (self.source_code or "").strip():
            raise ValueError("code drafts need source_code")
        return self


class DraftUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    spec: dict[str, Any] | None = None
    source_code: str | None = Field(default=None, max_length=_MAX_SOURCE_CHARS)


class Draft(BaseModel):
    id: str
    name: str
    kind: DraftKind
    spec: dict[str, Any]
    #: ``None`` for rule drafts, and for code drafts while code strategies
    #: are disabled.
    source_code: str | None
    status: DraftStatus
    registered_strategy_id: str | None
    #: Registry status of the registered strategy, when there is one.
    strategy_status: StrategyStatus | None
    created_at: str
    updated_at: str


class ValidationIssue(BaseModel):
    path: str
    message: str


class SmokeCheck(BaseModel):
    ok: bool
    data: Literal["lake", "sample"]
    tickers: list[str]
    #: ``estimate_return`` calls made / how many returned a pick.
    evaluations: int
    signals: int
    errors: list[str]


class DraftValidation(BaseModel):
    valid: bool
    issues: list[ValidationIssue]
    smoke: SmokeCheck | None = None


class ValidateRequest(BaseModel):
    """Smoke-run on these lake tickers (sample data when empty), over the
    last ``bars`` bars up to ``as_of`` (latest data when omitted)."""

    tickers: list[str] = Field(default_factory=list, max_length=20)
    as_of: date | None = None
    bars: int = Field(default=_SAMPLE_EVAL_BARS, ge=1, le=250)


class SpecValidateRequest(BaseModel):
    spec: dict[str, Any]


class _Window(BaseModel):
    universe: list[str] = Field(min_length=1)
    start: date
    end: date
    interval: str = "1d"

    @model_validator(mode="after")
    def _window(self) -> Self:
        if self.start >= self.end:
            raise ValueError("start must be before end")
        return self


class DraftBacktestRequest(_Window):
    """A :class:`~stonks.app.lab.BacktestRequest` without the strategy
    (the draft is the strategy)."""

    initial_cash: float = Field(default=10_000.0, gt=0)
    threshold: float = 0.0
    rebalance_every_bars: int = Field(default=1, ge=1)
    slippage_bps: float = Field(default=0.0, ge=0)
    fee_per_trade: float = Field(default=0.0, ge=0)


class DraftLabRunRequest(_Window):
    """A :class:`~stonks.app.lab.LabRunRequest` without the strategy. A
    rule draft's spec is fixed (it has no tunable parameters); a code
    draft's class is tuned over its parameter space."""

    train_ratio: float = Field(default=0.7, gt=0, lt=1)
    tuner: TunerName = "random"
    budget: int = Field(default=20, ge=1, le=1_000)
    seed: int = 0
    objective: ObjectiveName = "sharpe"
    survival_tests: list[SurvivalTestName] = Field(
        default_factory=lambda: ["oos", "period_stability"], min_length=1
    )
    #: Register the result (status ``shadow``) and link it to the draft.
    register_strategy: bool = False


# ---- service ----------------------------------------------------------------


class StudioService:
    def __init__(self, context: AppContext, lab: LabService, runner: JobRunner) -> None:
        self._ctx = context
        self._lab = lab
        self._runner = runner
        runner.register(STUDIO_BACKTEST_JOB, self._handle_code_backtest)
        runner.register(STUDIO_LAB_RUN_JOB, self._handle_lab_run)

    # ---- reads -------------------------------------------------------------

    def templates(self) -> list[RuleTemplateView]:
        return [
            RuleTemplateView(id=t.id, title=t.title, description=t.description, spec=t.spec)
            for t in TEMPLATES.values()
        ]

    def schema(self) -> dict[str, Any]:
        return rule_spec_json_schema()

    def validate_spec(self, spec: dict[str, Any]) -> DraftValidation:
        try:
            validate_spec(spec)
        except RuleSpecError as exc:
            return DraftValidation(valid=False, issues=_issues(exc))
        return DraftValidation(valid=True, issues=[])

    # ---- drafts CRUD -------------------------------------------------------

    def list_drafts(self, *, limit: int, offset: int) -> Page[Draft]:
        with self._ctx.state() as state:
            total = int(state.sql("SELECT COUNT(*) FROM strategy_drafts")[0][0])
            rows = state.sql(
                "SELECT * FROM strategy_drafts ORDER BY created_at DESC, rowid DESC "
                "LIMIT ? OFFSET ?",
                [limit, offset],
            )
            items = [self._view(state, row) for row in rows]
        return Page[Draft](items=items, total=total, limit=limit, offset=offset)

    def get_draft(self, draft_id: str) -> Draft:
        with self._ctx.state() as state:
            row = self._row(state, draft_id)
            return self._view(state, row)

    def create_draft(self, body: DraftCreate) -> Draft:
        self._guard(body.kind)
        draft_id = f"draft_{uuid.uuid4().hex[:12]}"
        now = _now()
        with self._ctx.state() as state:
            state.execute(
                "INSERT INTO strategy_drafts (id, name, kind, spec_json, source_code, status, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'draft', ?, ?)",
                [
                    draft_id,
                    body.name,
                    body.kind,
                    json.dumps(to_jsonable(body.spec), sort_keys=True),
                    body.source_code,
                    now,
                    now,
                ],
            )
        _log.info("studio.draft_created", draft_id=draft_id, kind=body.kind)
        return self.get_draft(draft_id)

    def update_draft(self, draft_id: str, body: DraftUpdate) -> Draft:
        with self._ctx.state() as state:
            row = self._row(state, draft_id)
            if body.source_code is not None and row["kind"] != "code":
                raise ValidationError("source_code is only allowed on code drafts")
            sets: list[str] = []
            params: list[Any] = []
            if body.name is not None:
                if not body.name.strip():
                    raise ValidationError("name must not be blank")
                sets.append("name = ?")
                params.append(body.name.strip())
            if body.spec is not None:
                sets.append("spec_json = ?")
                params.append(json.dumps(to_jsonable(body.spec), sort_keys=True))
            if body.source_code is not None:
                if not body.source_code.strip():
                    raise ValidationError("source_code must not be blank")
                sets.append("source_code = ?")
                params.append(body.source_code)
            sets.append("updated_at = ?")
            params.append(_now())
            state.execute(
                f"UPDATE strategy_drafts SET {', '.join(sets)} WHERE id = ?", [*params, draft_id]
            )
        return self.get_draft(draft_id)

    def delete_draft(self, draft_id: str) -> Draft:
        """Delete the draft; a strategy registered from it stays registered."""
        draft = self.get_draft(draft_id)
        with self._ctx.state() as state:
            state.execute("DELETE FROM strategy_drafts WHERE id = ?", [draft_id])
        _log.info("studio.draft_deleted", draft_id=draft_id)
        return draft

    # ---- validate ----------------------------------------------------------

    def validate_draft(self, draft_id: str, request: ValidateRequest) -> DraftValidation:
        draft = self.get_draft(draft_id)
        if draft.kind == "code":
            try:
                loaded = self._load_code(draft)
            except ValidationError as exc:
                return DraftValidation(
                    valid=False, issues=[ValidationIssue(path="source_code", message=str(exc))]
                )
            smoke = _smoke_sample(lambda: loaded.cls(dict(draft.spec)))
            issues = [ValidationIssue(path="source_code", message=e) for e in smoke.errors]
            return DraftValidation(valid=smoke.ok, issues=issues, smoke=smoke)

        try:
            validate_spec(draft.spec)
        except RuleSpecError as exc:
            return DraftValidation(valid=False, issues=_issues(exc))
        factory = lambda: RuleStrategy({"spec": draft.spec})  # noqa: E731
        if request.tickers and Path(self._ctx.settings.lake.path).exists():
            smoke = self._smoke_lake(factory, request)
        else:
            smoke = _smoke_sample(factory, request.bars)
        issues = [ValidationIssue(path="", message=e) for e in smoke.errors]
        return DraftValidation(valid=smoke.ok, issues=issues, smoke=smoke)

    # ---- backtest + lab run ------------------------------------------------

    def submit_backtest(self, draft_id: str, request: DraftBacktestRequest) -> Job:
        draft = self.get_draft(draft_id)
        _parse_interval(request.interval)
        if draft.kind == "rule":
            spec = _valid_spec(draft.spec)
            ref = StrategyRef(class_path=RULE_STRATEGY_CLASS_PATH, params={"spec": spec})
            return self._lab.submit_backtest(BacktestRequest(strategy=ref, **request.model_dump()))
        loaded = self._checked_code(draft)
        return self._runner.submit(
            STUDIO_BACKTEST_JOB,
            {
                "draft_id": draft.id,
                "module": loaded.module_name,
                "path": loaded.path.name,
                "params": draft.spec,
                "request": request.model_dump(mode="json"),
            },
        )

    def submit_lab_run(self, draft_id: str, request: DraftLabRunRequest) -> Job:
        draft = self.get_draft(draft_id)
        _parse_interval(request.interval)
        if request.register_strategy and draft.status == "registered":
            raise ConflictError(
                f"draft {draft_id} is already registered as {draft.registered_strategy_id}"
            )
        params: dict[str, Any] = {
            "draft_id": draft.id,
            "kind": draft.kind,
            "request": request.model_dump(mode="json"),
        }
        if draft.kind == "rule":
            params["spec"] = _valid_spec(draft.spec)
        else:
            loaded = self._checked_code(draft)
            params |= {"module": loaded.module_name, "path": loaded.path.name}
        return self._runner.submit(STUDIO_LAB_RUN_JOB, params)

    # ---- registration + lifecycle -----------------------------------------

    def register_draft(self, draft_id: str) -> Draft:
        """Register the draft's strategy (no survival reports) in ``shadow``
        — the same registry path a lab run with ``register_strategy`` takes."""
        draft = self.get_draft(draft_id)
        if draft.status == "registered":
            raise ConflictError(
                f"draft {draft_id} is already registered as {draft.registered_strategy_id}"
            )
        if draft.kind == "rule":
            strategy: Any = RuleStrategy({"spec": _valid_spec(draft.spec)})
        else:
            strategy = self._checked_code(draft).cls(dict(draft.spec))
        self._register(draft.id, draft.name, strategy, [])
        return self.get_draft(draft_id)

    def enable(self, draft_id: str) -> Draft:
        return self._set_status(draft_id, "active")

    def disable(self, draft_id: str) -> Draft:
        return self._set_status(draft_id, "shadow")

    def user_strategy_class(self, class_path: str) -> type:
        """Resolve a registered code strategy's ``stonks_user_strategies.<stem>:Class``
        path from the user strategies directory. The hook a registry /
        catalog loader calls for that namespace; refuses while code
        strategies are disabled."""
        self._require_code_enabled()
        module_name, _, cls_name = class_path.partition(":")
        cls = self._import_user_class(module_name, f"{module_name.rsplit('.', 1)[-1]}.py")
        if cls.__name__ != cls_name:
            raise ValidationError(f"{module_name} defines {cls.__name__}, not {cls_name}")
        return cls

    # ---- job handlers ------------------------------------------------------

    def _handle_code_backtest(self, params: dict[str, Any], ctx: JobContext) -> BacktestResult:
        self._require_code_enabled()
        cls = self._import_user_class(params["module"], params["path"])
        request = DraftBacktestRequest.model_validate(params["request"])
        return self._run_backtest(cls(dict(params.get("params") or {})), request)

    def _handle_lab_run(self, params: dict[str, Any], ctx: JobContext) -> LabRunView:
        request = DraftLabRunRequest.model_validate(params["request"])
        if params["kind"] == "rule":
            cls: type = RuleStrategy.bind(params["spec"])
        else:
            self._require_code_enabled()
            cls = self._import_user_class(params["module"], params["path"])
        interval = _parse_interval(request.interval)
        tuner: Tuner = (
            GridTuner(seed=request.seed) if request.tuner == "grid" else RandomTuner(request.seed)
        )
        runner = LabRunner(
            tuner=tuner,
            objective=_OBJECTIVES[request.objective](),
            suite=SurvivalSuite([_SURVIVAL_TESTS[t]() for t in request.survival_tests]),
            budget=request.budget,
        )
        ctx.progress(0.05, "tuning")
        with self._ctx.lake() as lake:
            dataset = LabDataset(
                lake=lake,
                universe=list(request.universe),
                start=request.start,
                end=request.end,
                train_ratio=request.train_ratio,
                interval=interval,
            )
            result = runner.run(cls, dataset)

        registered: str | None = None
        if request.register_strategy:
            ctx.progress(0.95, "registering")
            name = self._draft_name(params["draft_id"])
            registered = self._register(
                params["draft_id"], name, result.strategy, result.survival_reports
            )
        return LabRunView(
            class_path=_class_path(result.strategy),
            best_params=to_jsonable(result.best_params),
            best_score=finite(result.best_score),
            verdict=result.verdict,  # type: ignore[arg-type]
            survival_reports=[
                SurvivalReportView(
                    test_id=r.test_id,
                    passed=r.passed,
                    metrics={k: finite(v) for k, v in dict(r.metrics).items()},
                    notes=r.notes,
                )
                for r in result.survival_reports
            ],
            registered_strategy_id=registered,
        )

    # ---- internals: drafts -------------------------------------------------

    def _guard(self, kind: str) -> None:
        if kind == "code":
            self._require_code_enabled()

    def _require_code_enabled(self) -> None:
        if not self._ctx.settings.api.allow_code_strategies:
            raise CodeStrategiesDisabledError(
                "code strategies are disabled; set [api] allow_code_strategies = true to "
                "allow running user Python code (it runs with the server's privileges)"
            )

    def _row(self, state: Any, draft_id: str) -> Any:
        rows = state.sql("SELECT * FROM strategy_drafts WHERE id = ?", [draft_id])
        if not rows:
            raise NotFoundError(f"no draft with id {draft_id!r}")
        self._guard(rows[0]["kind"])
        return rows[0]

    def _view(self, state: Any, row: Any) -> Draft:
        sid = row["registered_strategy_id"]
        status = None
        if sid:
            found = state.sql("SELECT status FROM strategies WHERE id = ?", [sid])
            status = found[0]["status"] if found else None
        show_source = row["kind"] == "code" and self._ctx.settings.api.allow_code_strategies
        return Draft(
            id=row["id"],
            name=row["name"],
            kind=row["kind"],
            spec=json.loads(row["spec_json"] or "{}"),
            source_code=row["source_code"] if show_source else None,
            status=row["status"],
            registered_strategy_id=sid,
            strategy_status=status,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _draft_name(self, draft_id: str) -> str:
        with self._ctx.state() as state:
            rows = state.sql("SELECT name FROM strategy_drafts WHERE id = ?", [draft_id])
        return rows[0]["name"] if rows else draft_id

    def _register(self, draft_id: str, name: str, strategy: Any, reports: list[Any]) -> str:
        sid = f"{_slug(name)}_{uuid.uuid4().hex[:8]}"
        with self._ctx.state() as state:
            # Claim the draft first (a guarded single UPDATE), so two
            # concurrent registrations (a lab-run job and a direct register)
            # can't both register it.
            claimed = state.execute(
                "UPDATE strategy_drafts SET status = 'registered', registered_strategy_id = ?, "
                "updated_at = ? WHERE id = ? AND status = 'draft'",
                [sid, _now(), draft_id],
            )
            if claimed.rowcount != 1:
                raise ConflictError(f"draft {draft_id} is already registered (or was deleted)")
            try:
                self._ctx.registry_on(state).register(strategy, reports, strategy_id=sid)
            except BaseException:
                state.execute(
                    "UPDATE strategy_drafts SET status = 'draft', registered_strategy_id = NULL "
                    "WHERE id = ? AND registered_strategy_id = ?",
                    [draft_id, sid],
                )
                raise
        _log.info("studio.draft_registered", draft_id=draft_id, strategy_id=sid)
        return sid

    def _set_status(self, draft_id: str, status: StrategyStatus) -> Draft:
        draft = self.get_draft(draft_id)
        if draft.registered_strategy_id is None:
            raise ConflictError(f"draft {draft_id} is not registered yet")
        with self._ctx.registry() as registry:
            try:
                registry.set_status(draft.registered_strategy_id, status)
            except KeyError:
                raise NotFoundError(
                    f"registered strategy {draft.registered_strategy_id!r} no longer exists"
                ) from None
        _log.info(
            "studio.strategy_status",
            draft_id=draft_id,
            strategy_id=draft.registered_strategy_id,
            status=status,
        )
        return self.get_draft(draft_id)

    # ---- internals: running ------------------------------------------------

    def _run_backtest(self, strategy: Any, request: DraftBacktestRequest) -> BacktestResult:
        interval = _parse_interval(request.interval)
        broker = SimulatedBroker(
            portfolio=Portfolio(cash=request.initial_cash, positions={}),
            slippage_bps=request.slippage_bps,
            fee_per_trade=request.fee_per_trade,
        )
        config = BacktestConfig(
            start=request.start,
            end=request.end,
            universe=list(request.universe),
            interval=interval,
            threshold=request.threshold,
            rebalance_every_bars=request.rebalance_every_bars,
        )
        with self._ctx.lake() as lake:
            report = Backtester(
                strategies=[strategy], broker=broker, lake=lake, config=config
            ).run()
        return BacktestResult(
            strategy_id=report.strategy_id,
            interval=interval.code,
            start=request.start,
            end=request.end,
            final_return=finite(report.final_return),
            sharpe=finite(report.sharpe),
            max_drawdown=finite(report.max_drawdown),
            cagr=finite(report.cagr),
            profit_factor=finite(report.profit_factor),
            equity=[
                EquityPoint(timestamp=_as_datetime(ts), value=float(v))
                for ts, v in zip(report.equity_dates, report.equity_curve, strict=True)
            ],
        )

    def _smoke_lake(self, factory: Callable[[], Any], request: ValidateRequest) -> SmokeCheck:
        errors: list[str] = []
        evaluations = signals = 0
        try:
            strategy = factory()
        except Exception as exc:
            return _failed_smoke("lake", list(request.tickers), exc)
        interval = Interval.parse(strategy.spec.interval)
        end = request.as_of or date(2200, 1, 1)
        with self._ctx.lake() as lake:
            for ticker in request.tickers:
                bars = lake.get_bars(ticker, interval, start=datetime(1900, 1, 1), end=end)
                if bars is None or bars.empty:
                    errors.append(f"{ticker}: no {interval.code} bars in the lake")
                    continue
                for ts in bars["timestamp"].iloc[-request.bars :]:
                    try:
                        r = strategy.estimate_return(ticker, _as_datetime(ts), lake)
                    except Exception as exc:
                        errors.append(f"{ticker}: {type(exc).__name__}: {exc}")
                        break
                    evaluations += 1
                    signals += r is not None
        return SmokeCheck(
            ok=not errors,
            data="lake",
            tickers=list(request.tickers),
            evaluations=evaluations,
            signals=signals,
            errors=errors,
        )

    # ---- internals: code drafts --------------------------------------------

    def _checked_code(self, draft: Draft) -> _LoadedCode:
        """Load a code draft and run its smoke check; ValidationError on
        any failure."""
        loaded = self._load_code(draft)
        smoke = _smoke_sample(lambda: loaded.cls(dict(draft.spec)))
        if not smoke.ok:
            raise ValidationError(f"code draft failed its smoke check: {'; '.join(smoke.errors)}")
        return loaded

    def _load_code(self, draft: Draft) -> _LoadedCode:
        self._require_code_enabled()
        source = draft.source_code or ""
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
        stem = f"{_slug(draft.id)}_{digest}"
        directory = user_strategies_dir(self._ctx.settings)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{stem}.py"
        if not path.exists():
            path.write_text(source, encoding="utf-8", newline="\n")
        module_name = f"{USER_MODULE_PREFIX}.{stem}"
        cls = self._import_user_class(module_name, path.name)
        return _LoadedCode(module_name=module_name, path=path, cls=cls)

    def _import_user_class(self, module_name: str, file_name: str) -> type:
        """Import ``file_name`` from the user strategies directory as
        ``module_name`` and return its single BaseStrategy subclass."""
        self._require_code_enabled()
        directory = user_strategies_dir(self._ctx.settings).resolve()
        path = (directory / file_name).resolve()
        if (
            not _MODULE_NAME.match(module_name)
            or path.parent != directory
            or path.suffix != ".py"
            or path.stem != module_name.rsplit(".", 1)[1]
        ):
            raise ValidationError("refusing to import a module outside the user strategies dir")
        if not path.is_file():
            raise ValidationError(f"user strategy file {path.name} is missing")
        module = sys.modules.get(module_name)
        if module is None:
            module = _exec_module(module_name, path)
        classes = [
            obj
            for obj in vars(module).values()
            if inspect.isclass(obj)
            and issubclass(obj, BaseStrategy)
            and obj.__module__ == module_name
        ]
        if len(classes) != 1:
            names = ", ".join(c.__name__ for c in classes) or "none"
            raise ValidationError(
                f"a code strategy must define exactly one BaseStrategy subclass (found {names})"
            )
        return classes[0]


@dataclass(frozen=True)
class _LoadedCode:
    module_name: str
    path: Path
    cls: type


# ---- helpers ----------------------------------------------------------------

_exec_lock = threading.Lock()


def _exec_module(module_name: str, path: Path) -> ModuleType:
    with _exec_lock:
        existing = sys.modules.get(module_name)
        if existing is not None:
            return existing
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ValidationError(f"cannot load {path.name}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException as exc:
            sys.modules.pop(module_name, None)
            if isinstance(exc, KeyboardInterrupt | SystemExit):
                raise ValidationError(f"{type(exc).__name__} raised while importing") from None
            if isinstance(exc, Exception):
                raise ValidationError(f"{type(exc).__name__}: {exc}") from None
            raise
        return module


def _smoke_sample(factory: Callable[[], Any], bars: int = _SAMPLE_EVAL_BARS) -> SmokeCheck:
    """Construct the strategy, check its parameter spec, run
    ``estimate_return`` over the last ``bars`` synthetic bars of two sample
    tickers and ``decide`` once."""
    tickers = list(_SAMPLE_TICKERS)
    frames = {t: synthetic_bars(_SAMPLE_BARS, seed=i + 1) for i, t in enumerate(tickers)}
    lake = SampleLake(frames)
    try:
        strategy = factory()
        spec = type(strategy).parameter_spec()
        defaults = {p.name: p.default for p in spec}
        validate_params(defaults, spec)
    except Exception as exc:
        return _failed_smoke("sample", tickers, exc)

    errors: list[str] = []
    evaluations = signals = 0
    picks: list[tuple[float, str]] = []
    stamps = frames[tickers[0]]["timestamp"].iloc[-bars:]
    as_of = _as_datetime(stamps.iloc[-1])
    for ticker in tickers:
        for ts in stamps:
            try:
                r = strategy.estimate_return(ticker, _as_datetime(ts), lake)
            except Exception as exc:
                errors.append(f"estimate_return raised {type(exc).__name__}: {exc}")
                break
            evaluations += 1
            if r is None:
                continue
            if not isinstance(r, int | float) or isinstance(r, bool) or not math.isfinite(r):
                errors.append(f"estimate_return must return a finite float or None, got {r!r}")
                break
            signals += 1
            if ts == stamps.iloc[-1]:
                picks.append((float(r), ticker))
        if errors:
            break
    if not errors:
        prices = {t: float(f["close"].iloc[-1]) for t, f in frames.items()}
        try:
            orders = strategy.decide(
                sorted(picks, reverse=True), Portfolio(cash=10_000.0), prices, as_of
            )
            if not isinstance(orders, list) or not all(isinstance(o, Order) for o in orders):
                errors.append("decide must return a list of Order")
        except Exception as exc:
            errors.append(f"decide raised {type(exc).__name__}: {exc}")
    return SmokeCheck(
        ok=not errors,
        data="sample",
        tickers=tickers,
        evaluations=evaluations,
        signals=signals,
        errors=errors,
    )


def _failed_smoke(
    data: Literal["lake", "sample"], tickers: list[str], exc: Exception
) -> SmokeCheck:
    if isinstance(exc, RuleSpecError):
        message = str(exc)
    else:
        message = f"could not build the strategy: {type(exc).__name__}: {exc}"
    return SmokeCheck(
        ok=False, data=data, tickers=tickers, evaluations=0, signals=0, errors=[message]
    )


def _issues(exc: RuleSpecError) -> list[ValidationIssue]:
    return [ValidationIssue(path=i.path, message=i.message) for i in exc.issues]


def _valid_spec(spec: dict[str, Any]) -> dict[str, Any]:
    try:
        return validate_spec(spec).model_dump(mode="json")
    except RuleSpecError as exc:
        raise ValidationError(str(exc)) from None


def _class_path(strategy: Any) -> str:
    cls = type(strategy)
    return f"{cls.__module__}:{cls.__name__}"


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return (slug or "strategy")[:40].rstrip("_") or "strategy"


def _parse_interval(code: str) -> Interval:
    try:
        return Interval.parse(code)
    except (ValueError, TypeError) as exc:
        raise ValidationError(f"invalid interval {code!r}: {exc}") from None


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if hasattr(value, "to_pydatetime"):
        return value.to_pydatetime()
    return datetime(value.year, value.month, value.day)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


# ---- wiring -----------------------------------------------------------------

_wiring_lock = threading.Lock()


def studio_service(services: Any) -> StudioService:
    """The :class:`StudioService` of a ``Services`` container, created on
    first use (and cached on it) until ``Services.create`` wires one in."""
    existing = getattr(services, "studio", None)
    if isinstance(existing, StudioService):
        return existing
    with _wiring_lock:
        existing = getattr(services, "studio", None)
        if not isinstance(existing, StudioService):
            existing = StudioService(services.context, services.lab, services.runner)
            services.studio = existing
    return existing
