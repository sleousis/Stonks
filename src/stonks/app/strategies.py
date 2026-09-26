"""StrategyService — registered strategies: list, detail, lifecycle status,
and resolving a :class:`StrategyRef` into a runnable strategy instance.

Status changes (BL-24) go through :func:`change_status`, the one service-level
entry point shared by every transport (API, MCP via the API, CLI, Studio): it
evaluates the go-live gate for a promotion and hands the report to
``StrategyRegistry.set_status``, which enforces the rules and writes the
audit row. Views carry each strategy's metadata card (BL-26) and history.
"""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks.app.catalog import CatalogService, class_path_of
from stonks.app.context import AppContext
from stonks.app.errors import ConflictError, NotFoundError, ValidationError
from stonks.app.pagination import Page
from stonks.app.serialize import FiniteFloat, finite, to_jsonable
from stonks.core.protocols import Strategy
from stonks.logging import get_logger
from stonks.production.golive import evaluate_golive
from stonks.registry.store import (
    GovernanceError,
    PromotionRefused,
    StatusChange,
    StrategyHandle,
)
from stonks.strategies.base import AlphaFamily, Premise, StrategyMetadata, strategy_metadata

StrategyStatus = Literal["active", "shadow", "retired"]
_STATUSES = ("active", "shadow", "retired")

_log = get_logger("stonks.app.strategies")


class PromotionRefusedError(ConflictError):
    """A promotion the go-live gate refused (HTTP 409). ``failures`` lists
    the failing go-live checks as ``"name: detail"`` lines, so transports
    can show them one per line."""

    def __init__(self, message: str, failures: list[str] | None = None) -> None:
        super().__init__(message)
        self.failures = list(failures or [])


class StrategyRef(BaseModel):
    """Which strategy to run: a registered id, or a catalog class + params.

    Exactly one of ``strategy_id`` / ``class_path`` is set. Future sources
    of runnable strategies (e.g. a studio draft id) become one more
    alternative here and one more branch in :meth:`StrategyService.resolve`.
    """

    strategy_id: str | None = None
    class_path: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.strategy_id is None) == (self.class_path is None):
            raise ValueError("set exactly one of strategy_id or class_path")
        return self


class SurvivalReportView(BaseModel):
    test_id: str
    passed: bool
    metrics: dict[str, FiniteFloat]
    notes: str


class StrategyMetadataView(BaseModel):
    """The strategy's hypothesis card and capability hooks (BL-26)."""

    hypothesis: str
    alpha_family: AlphaFamily
    premise: Premise
    label_horizon_bars: int
    required_history_bars: int


class StatusChangeView(BaseModel):
    """One audited status change or intervention (BL-24)."""

    id: int
    kind: str
    from_status: str | None
    to_status: str | None
    actor: str
    reason: str
    override: bool
    golive_passed: bool | None
    golive_report: dict[str, Any] | None
    created_at: str


class StatusChangeRequest(BaseModel):
    """Body of a status-change route (promote / retire / shadow, Studio
    enable / disable). Demotions need ``reason``; a promotion without a
    passing go-live check needs ``override`` plus a ``reason`` of at least
    20 characters."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=2_000)
    override: bool = False
    #: Who asked (logged); defaults to the transport (``api``, ``studio``).
    actor: str | None = Field(default=None, min_length=1, max_length=100)


class StrategySummary(BaseModel):
    id: str
    class_path: str
    status: StrategyStatus
    params: dict[str, Any]
    applicable_asset_classes: list[str]
    metadata: StrategyMetadataView
    created_at: str
    updated_at: str


class StrategyDetail(StrategySummary):
    survival_reports: list[SurvivalReportView]
    status_history: list[StatusChangeView]


class StrategyStatusCounts(BaseModel):
    """How many registered strategies are in each lifecycle status."""

    active: int
    shadow: int
    retired: int
    total: int


class StrategyService:
    def __init__(self, context: AppContext, catalog: CatalogService) -> None:
        self._ctx = context
        self._catalog = catalog

    def list(
        self, *, status: str | None = None, q: str | None = None, limit: int, offset: int
    ) -> Page[StrategySummary]:
        """``q`` keeps strategies whose id or class path contains it
        (case-insensitive)."""
        if status is not None and status not in _STATUSES:
            raise ValidationError(f"status must be one of {list(_STATUSES)}, got {status!r}")
        with self._ctx.registry() as registry:
            handles = registry.list_all(status=status)
        needle = (q or "").strip().lower()
        if needle:
            handles = [
                h for h in handles if needle in h.id.lower() or needle in h.class_path.lower()
            ]
        items = [self._summary(h) for h in handles[offset : offset + limit]]
        return Page[StrategySummary](items=items, total=len(handles), limit=limit, offset=offset)

    def counts(self) -> StrategyStatusCounts:
        with self._ctx.state() as state:
            rows = state.sql("SELECT status, COUNT(*) AS n FROM strategies GROUP BY status")
        by_status = dict.fromkeys(_STATUSES, 0) | {r["status"]: int(r["n"]) for r in rows}
        return StrategyStatusCounts(
            active=by_status["active"],
            shadow=by_status["shadow"],
            retired=by_status["retired"],
            total=sum(by_status.values()),
        )

    def get(self, strategy_id: str) -> StrategyDetail:
        with self._ctx.registry() as registry:
            handle = next((h for h in registry.list_all() if h.id == strategy_id), None)
            if handle is None:
                raise NotFoundError(f"no strategy with id {strategy_id!r}")
            reports = registry.get_reports(strategy_id)
            history = registry.status_history(strategy_id)
        return StrategyDetail(
            **self._summary(handle).model_dump(),
            survival_reports=[
                SurvivalReportView(
                    test_id=r.test_id,
                    passed=r.passed,
                    metrics={k: finite(v) for k, v in dict(r.metrics).items()},
                    notes=r.notes,
                )
                for r in reports
            ],
            status_history=[_change_view(c) for c in history],
        )

    def history(self, strategy_id: str) -> list[StatusChangeView]:
        """The strategy's audited status changes, oldest first."""
        return self.get(strategy_id).status_history

    def set_status(
        self,
        strategy_id: str,
        status: StrategyStatus,
        *,
        actor: str = "api",
        reason: str | None = None,
        override: bool = False,
    ) -> StrategyDetail:
        change_status(self._ctx, strategy_id, status, actor=actor, reason=reason, override=override)
        return self.get(strategy_id)

    def promote(
        self,
        strategy_id: str,
        *,
        actor: str = "api",
        reason: str | None = None,
        override: bool = False,
    ) -> StrategyDetail:
        """Move to ``active``: needs a passing go-live check, or
        ``override=True`` with a reason of at least 20 characters."""
        return self.set_status(strategy_id, "active", actor=actor, reason=reason, override=override)

    def retire(
        self, strategy_id: str, *, actor: str = "api", reason: str | None = None
    ) -> StrategyDetail:
        return self.set_status(strategy_id, "retired", actor=actor, reason=reason)

    def shadow(
        self, strategy_id: str, *, actor: str = "api", reason: str | None = None
    ) -> StrategyDetail:
        return self.set_status(strategy_id, "shadow", actor=actor, reason=reason)

    def promotion_warnings(self, strategy_id: str) -> list[str]:
        """Non-blocking concerns to show before promoting (e.g. an empty
        hypothesis card, P1)."""
        return _promotion_warnings(self.get(strategy_id).metadata)

    # ---- resolving refs ----------------------------------------------------

    def strategy_class(self, ref: StrategyRef) -> type:
        """The class a ref points at, without instantiating it."""
        if ref.class_path is not None:
            return self._catalog.strategy_class(ref.class_path)
        # A registered strategy was vetted when it was registered: load it
        # through the registry (like ``resolve``), not the catalog
        # allow-list, which only gates class paths sent by clients.
        return type(self._load_registered(ref.strategy_id or ""))

    def resolve(self, ref: StrategyRef) -> Strategy:
        """Instantiate the strategy a ref points at. Registered ids load
        through the registry (restoring fitted state); class paths must be
        offered by the catalog and get their params validated."""
        if ref.strategy_id is not None:
            return self._load_registered(ref.strategy_id)
        cls = self._catalog.strategy_class(ref.class_path or "")
        try:
            return cls(dict(ref.params))
        except (ValueError, TypeError, KeyError) as exc:
            raise ValidationError(f"invalid params for {class_path_of(cls)}: {exc}") from None

    # ---- internals ---------------------------------------------------------

    def _load_registered(self, strategy_id: str) -> Strategy:
        with self._ctx.registry() as registry:
            try:
                return registry.load(strategy_id)
            except KeyError:
                raise NotFoundError(f"no strategy with id {strategy_id!r}") from None

    def _summary(self, h: StrategyHandle) -> StrategySummary:
        meta = _describe(h.class_path, h.params)
        return StrategySummary(
            id=h.id,
            class_path=h.class_path,
            status=h.status,  # type: ignore[arg-type]
            params=to_jsonable(h.params),
            applicable_asset_classes=list(meta.applicable_asset_classes),
            metadata=StrategyMetadataView(
                hypothesis=meta.hypothesis,
                alpha_family=meta.alpha_family,
                premise=meta.premise,
                label_horizon_bars=meta.label_horizon_bars,
                required_history_bars=meta.required_history_bars,
            ),
            created_at=h.created_at,
            updated_at=h.updated_at,
        )


def change_status(
    ctx: AppContext,
    strategy_id: str,
    status: str,
    *,
    actor: str,
    reason: str | None = None,
    override: bool = False,
) -> StatusChange | None:
    """The service-level status change every transport uses.

    A move to ``active`` evaluates the go-live gate (``[golive]`` policy)
    and passes the report to the registry, which refuses the promotion
    unless it passed or ``override`` comes with a long enough reason; the
    report is stored with the audit row either way. Demotions need a
    reason. Maps registry errors onto service errors: unknown id ->
    ``NotFoundError``, refused promotion -> ``ConflictError``, a broken
    rule (missing actor / reason) -> ``ValidationError``.
    """
    if status not in _STATUSES:
        raise ValidationError(f"status must be one of {list(_STATUSES)}, got {status!r}")
    with ctx.state() as state:
        registry = ctx.registry_on(state)
        try:
            report = (
                evaluate_golive(state, registry, strategy_id, ctx.settings.golive)
                if status == "active"
                else None
            )
            change = registry.set_status(
                strategy_id,
                status,
                actor=actor,
                reason=reason,
                golive_report=report,
                override=override,
            )
        except KeyError:
            raise NotFoundError(f"no strategy with id {strategy_id!r}") from None
        except PromotionRefused as exc:
            raise PromotionRefusedError(str(exc), _failed_checks(report)) from None
        except GovernanceError as exc:
            raise ValidationError(str(exc)) from None
        if change is not None and status == "active":
            handle = next(h for h in registry.list_all() if h.id == strategy_id)
            for warning in _promotion_warnings(_describe(handle.class_path, handle.params)):
                _log.warning("strategy.promotion_warning", strategy_id=strategy_id, warning=warning)
    if change is not None:
        _log.info(
            "strategy.status_changed",
            strategy_id=strategy_id,
            from_status=change.from_status,
            status=status,
            actor=change.actor,
            override=change.override,
            golive_passed=change.golive_passed,
        )
    return change


def _failed_checks(report: Any) -> list[str]:
    return [
        f"{c.name}: {c.detail}" if c.detail else c.name
        for c in getattr(report, "checks", None) or []
        if not c.passed
    ]


def _promotion_warnings(meta: StrategyMetadata | StrategyMetadataView) -> list[str]:
    warnings = []
    if not meta.hypothesis.strip():
        warnings.append(
            "hypothesis is empty: state the mechanism, counterparty and failure mode (P1)"
        )
    return warnings


def _change_view(c: StatusChange) -> StatusChangeView:
    return StatusChangeView(
        id=c.id,
        kind=c.kind,
        from_status=c.from_status,
        to_status=c.to_status,
        actor=c.actor,
        reason=c.reason,
        override=c.override,
        golive_passed=c.golive_passed,
        golive_report=c.golive_report,
        created_at=c.created_at,
    )


def _describe(class_path: str, params: dict[str, Any]) -> StrategyMetadata:
    """The strategy's metadata, read off an instance built from its params
    when it can be (what it actually trades: ``RuleStrategy`` takes classes
    from its spec's universe, a wrapper from its inner strategy, any
    strategy from an ``asset_classes`` override), else off the class."""
    import importlib

    try:
        module_name, cls_name = class_path.split(":", 1)
        cls = getattr(importlib.import_module(module_name), cls_name)
    except Exception:
        return StrategyMetadata()
    try:
        return strategy_metadata(cls(dict(params)))
    except Exception:
        return strategy_metadata(cls)
