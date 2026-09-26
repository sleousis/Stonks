"""TickService — the production tick ledger and running a tick (optionally
dry-run), mirroring ``stonks tick``."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from stonks.app.context import AppContext
from stonks.app.errors import ConflictError, NotFoundError, ValidationError
from stonks.app.jobs import Job, JobContext, JobRunner
from stonks.app.orders import OrdersService, OrderView
from stonks.app.pagination import Page
from stonks.core.types import AssetClass
from stonks.production.settings_builder import build_tick_runtime
from stonks.production.tick import BackdatedTickError, run_tick

TICK_JOB = "tick"

#: ``tick_runs.status`` (its CHECK constraint). A no-op tick is ``ok`` with
#: ``summary.reason`` set.
TickStatus = Literal["running", "ok", "partial", "error"]

_TICK_ID_DATE = re.compile(r"^tick_(\d{4}-\d{2}-\d{2})_")


class TickRequest(BaseModel):
    dry_run: bool = False
    #: Defaults to today (UTC).
    as_of: date | None = None
    #: Overrides ``[production].universe``.
    tickers: list[str] | None = None
    asset_class: AssetClass | None = None


class TickRunView(BaseModel):
    id: str
    as_of: date | None = Field(
        default=None,
        description="The trading date the tick ran for (null for rows that predate it).",
    )
    started_at: datetime
    finished_at: datetime | None
    status: TickStatus
    summary: dict[str, Any] | None


class TickRunDetail(TickRunView):
    orders: list[OrderView]


class TickResultView(BaseModel):
    tick_id: str
    status: str
    winner_strategy_id: str | None
    orders_placed: int
    fills: int
    dry_run: bool


class TickService:
    def __init__(self, context: AppContext, orders: OrdersService, runner: JobRunner) -> None:
        self._ctx = context
        self._orders = orders
        self._runner = runner
        # ticks mutate the book; never run two at once
        runner.register(TICK_JOB, self._handle, lock="tick")

    def list(
        self, *, status: TickStatus | None = None, limit: int, offset: int
    ) -> Page[TickRunView]:
        clause = " WHERE status = ?" if status else ""
        params: list[Any] = [status] if status else []
        with self._ctx.state() as state:
            total = int(state.sql(f"SELECT COUNT(*) FROM tick_runs{clause}", params)[0][0])
            rows = state.sql(
                f"SELECT t.*, {_AS_OF_SQL} FROM tick_runs t{clause} "
                "ORDER BY started_at DESC, rowid DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            )
        return Page[TickRunView](
            items=[_row_to_view(r) for r in rows], total=total, limit=limit, offset=offset
        )

    def get(self, tick_id: str) -> TickRunDetail:
        with self._ctx.state() as state:
            rows = state.sql(f"SELECT t.*, {_AS_OF_SQL} FROM tick_runs t WHERE id = ?", [tick_id])
        if not rows:
            raise NotFoundError(f"no tick with id {tick_id!r}")
        orders = self._orders.orders(tick_id=tick_id, limit=10_000, offset=0).items
        return TickRunDetail(**_row_to_view(rows[0]).model_dump(), orders=orders)

    def submit(self, request: TickRequest) -> Job:
        self._universe(request)  # fail fast on an empty universe
        return self._runner.submit(TICK_JOB, request.model_dump(mode="json"))

    def run(self, request: TickRequest) -> TickResultView:
        universe = self._universe(request)
        with self._ctx.state() as state, self._ctx.lake() as lake:
            registry = self._ctx.registry_on(state)
            if request.asset_class is not None:
                classes = lake.get_asset_classes(universe)
                universe = [t for t in universe if classes.get(t) == request.asset_class]
                if not universe:
                    raise ValidationError(
                        f"no instruments in the universe match asset_class={request.asset_class!r}"
                    )
            runtime = build_tick_runtime(self._ctx.settings, universe)
            try:
                result = run_tick(
                    state=state,
                    lake=lake,
                    registry=registry,
                    settings=runtime.settings,
                    as_of=request.as_of,
                    dry_run=request.dry_run,
                    notifier=runtime.notifier,
                    broker_factory=runtime.broker_factory,
                    plan=runtime.plan_for(state),
                )
            except BackdatedTickError as exc:
                raise ConflictError(str(exc)) from None
        return TickResultView(
            tick_id=result.tick_id,
            status=result.status,
            winner_strategy_id=result.winner_strategy_id,
            orders_placed=result.orders_placed,
            fills=result.fills,
            dry_run=request.dry_run,
        )

    def _universe(self, request: TickRequest) -> list[str]:
        universe = list(request.tickers or self._ctx.settings.production.universe)
        if not universe:
            raise ValidationError(
                "production universe is empty; pass tickers or set [production].universe"
            )
        return universe

    def _handle(self, params: dict[str, Any], ctx: JobContext) -> TickResultView:
        return self.run(TickRequest.model_validate(params))


#: The tick's trading date as recorded on its portfolio snapshot (ticks that
#: traded); no-op and dry-run ticks fall back to the date in their id.
_AS_OF_SQL = (
    "(SELECT MAX(s.as_of) FROM portfolio_snapshots s WHERE s.tick_id = t.id) AS snapshot_as_of"
)


def _as_of(row: Any) -> date | None:
    for raw in (row["snapshot_as_of"], _id_date(row["id"])):
        if raw:
            try:
                return date.fromisoformat(raw)
            except ValueError:
                continue
    return None


def _id_date(tick_id: str) -> str | None:
    m = _TICK_ID_DATE.match(tick_id)
    return m.group(1) if m else None


def _row_to_view(row: Any) -> TickRunView:
    summary = row["summary_json"]
    return TickRunView(
        id=row["id"],
        as_of=_as_of(row),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        status=row["status"],
        summary=None if summary is None else json.loads(summary),
    )
