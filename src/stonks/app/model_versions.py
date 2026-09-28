"""ModelVersionService: model versions under one strategy id (roadmap 22.6).

Every transport goes through here: list a strategy's versions and their
event log, queue a retrain (job kind ``model_retrain``, what the scheduler's
``model_retrain`` action runs), read the swap check of a candidate, swap
it in or reject it. A swap evaluates the swap check (``[lifecycle.swap]``)
and hands the report to ``ModelVersionRegistry.swap``, which refuses it
unless the check passed or an override comes with a long enough reason.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from stonks.app.context import AppContext
from stonks.app.errors import ConflictError, NotFoundError, ValidationError
from stonks.app.jobs import Job, JobContext, JobRunner
from stonks.app.strategies import FailingCheck
from stonks.lifecycle.calibration import calibration_report
from stonks.lifecycle.check import SwapReport, evaluate_swap
from stonks.lifecycle.retrain import RetrainSummary, retrain_models
from stonks.logging import get_logger
from stonks.registry.store import GovernanceError
from stonks.registry.versions import (
    ModelVersion,
    ModelVersionRegistry,
    SwapRefused,
    VersionEvent,
)

RETRAIN_JOB = "model_retrain"

_log = get_logger("stonks.app.model_versions")


class ModelVersionView(BaseModel):
    strategy_id: str
    version: int
    #: ``live``, ``candidate``, ``archived``, ``rejected`` or ``failed``.
    status: str
    #: The version's model book id (``<strategy>@v<n>``).
    book_id: str
    train_start: date | None
    train_end: date | None
    #: What the fit learned, as the strategy reports it.
    fit: dict[str, Any]
    error: str | None
    created_by: str
    created_at: str
    updated_at: str


class VersionEventView(BaseModel):
    """One row of the append-only version log."""

    id: int
    version: int
    kind: str
    from_status: str | None
    to_status: str
    actor: str
    reason: str
    override: bool
    check_passed: bool | None
    check_report: dict[str, Any] | None
    created_at: str


class SwapCheckView(BaseModel):
    name: str
    passed: bool
    value: float | None
    limit: float | None
    detail: str


class SwapReportView(BaseModel):
    """The swap check of a candidate against the live version (``[lifecycle.swap]``)."""

    strategy_id: str
    version: int
    live_version: int
    passed: bool
    #: Days in the candidate's model book.
    days: int
    candidate_return: float | None
    live_return: float | None
    candidate_drawdown: float | None
    checks: list[SwapCheckView]


class ReliabilityBinView(BaseModel):
    lower: float
    upper: float
    count: int
    mean_forecast: float | None
    observed_rate: float | None


class CalibrationView(BaseModel):
    """Live calibration of a classifier version's probability forecasts
    (roadmap 23.9): Brier score against always forecasting the base rate,
    the reliability table and the expected calibration error. Empty for a
    model that forecasts no probabilities."""

    strategy_id: str
    version: int
    n_forecasts: int
    n_resolved: int
    brier: float | None
    #: Brier score of always forecasting the observed base rate.
    brier_base_rate: float | None
    #: ``1 - brier / brier_base_rate``: above 0 beats the base rate.
    skill: float | None
    base_rate: float | None
    mean_forecast: float | None
    ece: float | None
    bins: list[ReliabilityBinView]


class RetrainRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Refit only these strategies (default: every retrainable strategy of
    #: ``[lifecycle].statuses``).
    strategy_ids: list[str] | None = Field(default=None, max_length=200)
    #: The last day of the training window (default: today).
    as_of: date | None = None
    #: Refit even when a recent fit exists.
    force: bool = False
    #: Tickers to fit on when the artifact records no lab universe
    #: (default: the production universe).
    tickers: list[str] | None = Field(default=None, max_length=5000)


class RetrainOutcomeView(BaseModel):
    strategy_id: str
    #: ``candidate``, ``failed`` or ``skipped``.
    status: str
    version: int | None
    detail: str | None
    train_start: date | None
    train_end: date | None


class RetrainResultView(BaseModel):
    as_of: date
    candidates: int
    failed: int
    skipped: int
    outcomes: list[RetrainOutcomeView]


class VersionChangeRequest(BaseModel):
    """Body of a swap or a reject. A reject needs ``reason``. A swap without
    a passing check needs ``override`` plus a ``reason`` of at least 20
    characters."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=2_000)
    override: bool = False


class SwapRefusedError(ConflictError):
    """A swap the check refused (HTTP 409), with the failing checks."""

    def __init__(
        self, message: str, failures: list[str], checks: list[FailingCheck] | None = None
    ) -> None:
        super().__init__(message)
        self.failures = failures
        self.checks = list(checks or [])

    def problem_extensions(self) -> dict[str, Any]:
        return {"failing_checks": [c.model_dump() for c in self.checks]}


class ModelVersionService:
    def __init__(self, context: AppContext, runner: JobRunner | None = None) -> None:
        """Without ``runner`` (the CLI) jobs can't be queued; ``retrain``
        runs in this process."""
        self._ctx = context
        self._runner = runner
        if runner is not None:
            runner.register(RETRAIN_JOB, self._handle, cancellable=True, operation=False)

    # ---- reads -----------------------------------------------------------------

    def list(self, strategy_id: str) -> list[ModelVersionView]:
        with self._versions() as versions:
            return [_view(v) for v in _found(lambda: versions.list(strategy_id), strategy_id)]

    def candidates(self) -> list[ModelVersionView]:
        """Every candidate of a strategy that is not retired."""
        with self._versions() as versions:
            return [_view(v) for v in versions.candidates()]

    def history(self, strategy_id: str) -> list[VersionEventView]:
        with self._versions() as versions:
            _found(lambda: versions.ensure_baseline(strategy_id), strategy_id)
            return [_event_view(e) for e in versions.history(strategy_id)]

    def check(self, strategy_id: str, version: int) -> SwapReportView:
        with self._ctx.state() as state:
            versions = self._on(state)
            report = _found(
                lambda: evaluate_swap(
                    state, versions, strategy_id, version, self._ctx.settings.lifecycle.swap
                ),
                f"{strategy_id} v{version}",
            )
        return _report_view(report)

    def calibration(self, strategy_id: str, version: int) -> CalibrationView:
        """Brier score and reliability of one version's live forecasts."""
        with self._ctx.state() as state:
            versions = self._on(state)
            _found(lambda: versions.get(strategy_id, version), f"{strategy_id} v{version}")
            report = calibration_report(state, strategy_id, version)
        return CalibrationView.model_validate(report.as_dict())

    # ---- governed writes ---------------------------------------------------------

    def swap(
        self,
        strategy_id: str,
        version: int,
        *,
        actor: str,
        reason: str | None = None,
        override: bool = False,
    ) -> ModelVersionView:
        """Make candidate ``version`` live after the swap check (or an override)."""
        with self._ctx.state() as state:
            versions = self._on(state)
            report = _found(
                lambda: evaluate_swap(
                    state, versions, strategy_id, version, self._ctx.settings.lifecycle.swap
                ),
                f"{strategy_id} v{version}",
            )
            try:
                event = versions.swap(
                    strategy_id,
                    version,
                    actor=actor,
                    reason=reason,
                    check_report=report,
                    override=override,
                )
            except SwapRefused as exc:
                failing = [c for c in report.checks if not c.passed]
                raise SwapRefusedError(
                    str(exc),
                    [f"{c.name}: {c.detail}" for c in failing],
                    [
                        FailingCheck(name=c.name, detail=c.detail, value=c.value, limit=c.limit)
                        for c in failing
                    ],
                ) from None
            except GovernanceError as exc:
                raise ValidationError(str(exc)) from None
            _log.info(
                "model_version.swapped",
                strategy_id=strategy_id,
                version=version,
                actor=event.actor,
                override=event.override,
                check_passed=event.check_passed,
            )
            return _view(versions.get(strategy_id, version))

    def reject(
        self, strategy_id: str, version: int, *, actor: str, reason: str | None
    ) -> ModelVersionView:
        with self._ctx.state() as state:
            versions = self._on(state)
            try:
                _found(
                    lambda: versions.reject(strategy_id, version, actor=actor, reason=reason or ""),
                    f"{strategy_id} v{version}",
                )
            except GovernanceError as exc:
                raise ValidationError(str(exc)) from None
            return _view(versions.get(strategy_id, version))

    # ---- retraining --------------------------------------------------------------

    def submit_retrain(
        self, request: RetrainRequest, *, actor: str, owner_id: str | None = None
    ) -> Job:
        if self._runner is None:  # pragma: no cover - wiring error
            raise ConflictError("no job runner: retrain in this process instead")
        params = {**request.model_dump(mode="json"), "actor": actor}
        return self._runner.submit(RETRAIN_JOB, params, owner_id=owner_id)

    def retrain(
        self,
        request: RetrainRequest,
        *,
        actor: str,
        checkpoint: Callable[[], None] | None = None,
    ) -> RetrainResultView:
        """Refit now, in this process (the job handler and the CLI)."""
        settings = self._ctx.settings
        as_of = request.as_of or datetime.now(UTC).date()
        with self._ctx.state() as state, self._ctx.lake() as lake:
            registry = self._ctx.registry_on(state)
            universe = request.tickers or _production_universe(lake, settings, as_of)
            try:
                summary = retrain_models(
                    state,
                    lake,
                    registry,
                    settings.lifecycle,
                    as_of=as_of,
                    universe=universe,
                    strategy_ids=request.strategy_ids,
                    force=request.force,
                    actor=actor,
                    parallel=settings.lab.parallel,
                    checkpoint=checkpoint,
                )
            except KeyError as exc:
                raise NotFoundError(f"no strategy with id {exc.args[0]!r}") from None
        return _result_view(summary)

    def _handle(self, params: dict[str, Any], ctx: JobContext) -> RetrainResultView:
        actor = str(params.pop("actor", None) or "api")
        return self.retrain(
            RetrainRequest.model_validate(params), actor=actor, checkpoint=ctx.check_cancelled
        )

    # ---- internals ---------------------------------------------------------------

    def _on(self, state: Any) -> ModelVersionRegistry:
        return ModelVersionRegistry.on(self._ctx.registry_on(state))

    @contextmanager
    def _versions(self) -> Iterator[ModelVersionRegistry]:
        with self._ctx.state() as state:
            yield self._on(state)


def _found[T](fn: Callable[[], T], what: str) -> T:
    try:
        return fn()
    except KeyError:
        raise NotFoundError(f"no strategy or version {what!r}") from None


def _production_universe(lake: Any, settings: Any, as_of: date) -> list[str]:
    configured = settings.production.universe
    if not isinstance(configured, str):
        return list(configured)
    from stonks.production.universe import production_tickers

    try:
        return production_tickers(lake, configured, as_of)
    except Exception as exc:  # an unrefreshed universe: fit on the lab universes only
        _log.warning("model_version.universe_unresolved", universe_id=configured, error=str(exc))
        return []


def _view(v: ModelVersion) -> ModelVersionView:
    return ModelVersionView(
        strategy_id=v.strategy_id,
        version=v.version,
        status=v.status,
        book_id=v.book_id,
        train_start=v.train_start,
        train_end=v.train_end,
        fit=v.fit,
        error=v.error,
        created_by=v.created_by,
        created_at=v.created_at,
        updated_at=v.updated_at,
    )


def _event_view(e: VersionEvent) -> VersionEventView:
    return VersionEventView(
        id=e.id,
        version=e.version,
        kind=e.kind,
        from_status=e.from_status,
        to_status=e.to_status,
        actor=e.actor,
        reason=e.reason,
        override=e.override,
        check_passed=e.check_passed,
        check_report=e.check_report,
        created_at=e.created_at,
    )


def _report_view(r: SwapReport) -> SwapReportView:
    return SwapReportView(
        strategy_id=r.strategy_id,
        version=r.version,
        live_version=r.live_version,
        passed=r.passed,
        days=r.days,
        candidate_return=r.candidate_return,
        live_return=r.live_return,
        candidate_drawdown=r.candidate_drawdown,
        checks=[SwapCheckView(**c.as_dict()) for c in r.checks],
    )


def _result_view(summary: RetrainSummary) -> RetrainResultView:
    return RetrainResultView.model_validate(summary.as_dict())
