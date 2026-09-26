"""StrategyService — registered strategies: list, detail, lifecycle status,
and resolving a :class:`StrategyRef` into a runnable strategy instance."""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, Field, model_validator

from stonks.app.catalog import CatalogService, class_path_of
from stonks.app.context import AppContext
from stonks.app.errors import NotFoundError, ValidationError
from stonks.app.pagination import Page
from stonks.app.serialize import finite, to_jsonable
from stonks.core.protocols import Strategy
from stonks.logging import get_logger
from stonks.registry.store import StrategyHandle

StrategyStatus = Literal["active", "shadow", "retired"]
_STATUSES = ("active", "shadow", "retired")

_log = get_logger("stonks.app.strategies")


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
    metrics: dict[str, float | None]
    notes: str


class StrategySummary(BaseModel):
    id: str
    class_path: str
    status: StrategyStatus
    params: dict[str, Any]
    applicable_asset_classes: list[str]
    created_at: str
    updated_at: str


class StrategyDetail(StrategySummary):
    survival_reports: list[SurvivalReportView]


class StrategyService:
    def __init__(self, context: AppContext, catalog: CatalogService) -> None:
        self._ctx = context
        self._catalog = catalog

    def list(self, *, status: str | None = None, limit: int, offset: int) -> Page[StrategySummary]:
        if status is not None and status not in _STATUSES:
            raise ValidationError(f"status must be one of {list(_STATUSES)}, got {status!r}")
        with self._ctx.registry() as registry:
            handles = registry.list_all(status=status)
        items = [self._summary(h) for h in handles[offset : offset + limit]]
        return Page[StrategySummary](items=items, total=len(handles), limit=limit, offset=offset)

    def get(self, strategy_id: str) -> StrategyDetail:
        with self._ctx.registry() as registry:
            handle = next((h for h in registry.list_all() if h.id == strategy_id), None)
            if handle is None:
                raise NotFoundError(f"no strategy with id {strategy_id!r}")
            reports = registry.get_reports(strategy_id)
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
        )

    def set_status(self, strategy_id: str, status: StrategyStatus) -> StrategyDetail:
        if status not in _STATUSES:
            raise ValidationError(f"status must be one of {list(_STATUSES)}, got {status!r}")
        with self._ctx.registry() as registry:
            try:
                registry.set_status(strategy_id, status)
            except KeyError:
                raise NotFoundError(f"no strategy with id {strategy_id!r}") from None
        _log.info("strategy.status_changed", strategy_id=strategy_id, status=status)
        return self.get(strategy_id)

    def promote(self, strategy_id: str) -> StrategyDetail:
        return self.set_status(strategy_id, "active")

    def retire(self, strategy_id: str) -> StrategyDetail:
        return self.set_status(strategy_id, "retired")

    def shadow(self, strategy_id: str) -> StrategyDetail:
        return self.set_status(strategy_id, "shadow")

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
        return StrategySummary(
            id=h.id,
            class_path=h.class_path,
            status=h.status,  # type: ignore[arg-type]
            params=to_jsonable(h.params),
            applicable_asset_classes=list(_applicable_classes(h.class_path, h.params)),
            created_at=h.created_at,
            updated_at=h.updated_at,
        )


def _applicable_classes(class_path: str, params: dict[str, Any]) -> tuple[str, ...]:
    """What the strategy actually trades: built from its params when it can
    be (``RuleStrategy`` takes them from its spec's universe, a
    ``MacroRegimeFilter`` from its inner strategy), else the class attribute."""
    import importlib

    try:
        module_name, cls_name = class_path.split(":", 1)
        cls = getattr(importlib.import_module(module_name), cls_name)
    except Exception:
        return ("equity",)
    try:
        return tuple(cls(dict(params)).applicable_asset_classes)
    except Exception:
        return tuple(getattr(cls, "applicable_asset_classes", ("equity",)))
