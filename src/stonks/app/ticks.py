"""TickService — the production tick ledger and running a tick (optionally
dry-run), mirroring ``stonks tick``."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from stonks.accounts import PortfolioRepository, Scope
from stonks.accounts.models import DEFAULT_PORTFOLIO_ID
from stonks.app.context import AppContext
from stonks.app.errors import ConflictError, NotFoundError, ValidationError
from stonks.app.jobs import Job, JobContext, JobRunner
from stonks.app.orders import OrdersService, OrderView
from stonks.app.pagination import Page
from stonks.auth.policy import Permission, require
from stonks.auth.principal import Principal
from stonks.core.types import AssetClass
from stonks.execution.brokers.base import BrokerMode
from stonks.production.settings_builder import build_tick_runtime
from stonks.production.tick import BackdatedTickError, TickResult, run_tick
from stonks.production.universe import EmptyUniverseError, production_tickers
from stonks.scheduling.calendar import bars_due

TICK_JOB = "tick"

#: ``tick_runs.status`` (its CHECK constraint). A no-op tick is ``ok`` with
#: ``summary.reason`` set.
TickStatus = Literal["running", "ok", "partial", "error"]

#: The caller: a :class:`Principal` (API, MCP) or a bare :class:`Scope`
#: (the CLI and in-process services).
Who = Scope | Principal

#: Orders fetched per page when a tick detail lists every order of a book.
_ORDER_PAGE = 1000

_TICK_ID_DATE = re.compile(r"^tick_(\d{4}-\d{2}-\d{2})_")


class TickRequest(BaseModel):
    dry_run: bool = False
    #: Defaults to today (UTC).
    as_of: date | None = None
    #: Overrides ``[production].universe``.
    tickers: list[str] | None = None
    asset_class: AssetClass | None = None
    scoped: bool | None = Field(
        default=None,
        description=(
            "Trade only the tick's tickers and leave other holdings alone, not even"
            " selling them. Default: true when tickers or asset_class narrow the"
            " universe."
        ),
    )

    bars_due_at: datetime | None = Field(
        default=None,
        description=(
            "Buy only tickers whose latest session that closed by this time has its"
            " daily bar in the lake (scheduled ticks send their fire time). Tickers"
            " whose bar is missing are marked and sellable, not buyable."
        ),
    )

    @property
    def is_scoped(self) -> bool:
        if self.scoped is not None:
            return self.scoped
        return bool(self.tickers) or self.asset_class is not None


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
    broker_mode: BrokerMode = Field(
        default="simulated",
        description=(
            "Whose money the default book traded: simulated fills, a broker's"
            " paper account, or a live (real money) account."
        ),
    )


class TickService:
    def __init__(self, context: AppContext, orders: OrdersService, runner: JobRunner) -> None:
        self._ctx = context
        self._orders = orders
        self._runner = runner
        # ticks mutate the book; never run two at once
        runner.register(TICK_JOB, self._handle, lock="tick")

    def list(
        self, who: Who, *, status: TickStatus | None = None, limit: int, offset: int
    ) -> Page[TickRunView]:
        """Every tick run, newest first, each summary cut to what ``who``
        may see (:func:`scope_summary`)."""
        clause = " WHERE status = ?" if status else ""
        params: list[Any] = [status] if status else []
        visible = self._visible(who)
        with self._ctx.state() as state:
            total = int(state.sql(f"SELECT COUNT(*) FROM tick_runs{clause}", params)[0][0])
            rows = state.sql(
                f"SELECT t.*, {_AS_OF_SQL} FROM tick_runs t{clause} "
                "ORDER BY started_at DESC, rowid DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            )
        return Page[TickRunView](
            items=[_row_to_view(r, visible) for r in rows],
            total=total,
            limit=limit,
            offset=offset,
        )

    def get(self, who: Who, tick_id: str, *, portfolio_id: str | None = None) -> TickRunDetail:
        """One tick run with the orders it placed in ``who``'s portfolios
        (only ``portfolio_id`` when given; another user's is a 404)."""
        visible = self._visible(who)
        if portfolio_id is not None and visible is not None and portfolio_id not in visible:
            raise NotFoundError(f"portfolio {portfolio_id!r} not found")
        with self._ctx.state() as state:
            rows = state.sql(f"SELECT t.*, {_AS_OF_SQL} FROM tick_runs t WHERE id = ?", [tick_id])
            if not rows:
                raise NotFoundError(f"no tick with id {tick_id!r}")
            if portfolio_id is not None:
                books = [portfolio_id]
            elif visible is not None:
                books = sorted(visible)
            else:
                books = _order_books(state, tick_id)
        orders = [o for pid in books for o in self._all_orders(tick_id, pid)]
        orders.sort(key=lambda o: str(o.created_at), reverse=True)
        return TickRunDetail(**_row_to_view(rows[0], visible).model_dump(), orders=orders)

    def _all_orders(self, tick_id: str, portfolio_id: str) -> list[OrderView]:
        """Every order of the tick in one book, page by page (no hard cap)."""
        out: list[OrderView] = []
        while True:
            page = self._orders.orders(
                tick_id=tick_id, portfolio_id=portfolio_id, limit=_ORDER_PAGE, offset=len(out)
            )
            out.extend(page.items)
            if not page.items or len(out) >= page.total:
                return out

    def _visible(self, who: Who) -> set[str] | None:
        """Ids of the portfolios ``who`` owns (``None``: a service, every
        portfolio). Admins see their own books like anyone else."""
        if isinstance(who, Principal):
            require(who, Permission.READ)
        scope = who.scope if isinstance(who, Principal) else who
        if scope.is_service:
            return None
        with self._ctx.state() as state:
            return {p.id for p in PortfolioRepository(state).list(scope)}

    def submit(self, request: TickRequest) -> Job:
        if not request.tickers and not self._ctx.settings.production.universe:
            self._universe(None, request)  # fail fast on an empty universe
        return self._runner.submit(TICK_JOB, request.model_dump(mode="json"))

    def run(self, request: TickRequest) -> TickResultView:
        with self._ctx.state() as state, self._ctx.lake() as lake:
            registry = self._ctx.registry_on(state)
            result = execute_tick(self._ctx.settings, state, lake, registry, request)
        return TickResultView(
            tick_id=result.tick_id,
            status=result.status,
            winner_strategy_id=result.winner_strategy_id,
            orders_placed=result.orders_placed,
            fills=result.fills,
            dry_run=result.dry_run,
            broker_mode=result.broker_mode,
        )

    def _universe(self, lake: Any, request: TickRequest) -> list[str]:
        return request_universe(self._ctx.settings, lake, request)

    def _handle(self, params: dict[str, Any], ctx: JobContext) -> TickResultView:
        return self.run(TickRequest.model_validate(params))


def request_universe(settings: Any, lake: Any, request: TickRequest) -> list[str]:
    """``request.tickers``, else ``[production].universe`` (a list, or a
    universe id resolved on the tick's date). ``ValidationError`` when empty."""
    as_of = request.as_of or datetime.now(UTC).date()
    try:
        return production_tickers(
            lake, settings.production.universe, as_of, tickers=request.tickers
        )
    except EmptyUniverseError as exc:
        raise ValidationError(str(exc)) from None


def execute_tick(
    settings: Any, state: Any, lake: Any, registry: Any, request: TickRequest
) -> TickResult:
    """One tick as every entrypoint runs it (the API job and ``stonks tick``,
    BE-66): the universe (with ``asset_class`` and the due bars), the
    runtime from settings, the per-portfolio plan, then :func:`run_tick`.
    ``ValidationError`` for an empty universe, ``ConflictError`` for a
    refused date (backdated or in the future)."""
    universe = request_universe(settings, lake, request)
    if request.asset_class is not None:
        classes = lake.get_asset_classes(universe)
        universe = [t for t in universe if classes.get(t) == request.asset_class]
        if not universe:
            raise ValidationError(
                f"no instruments in the universe match asset_class={request.asset_class!r}"
            )
    due = None
    if request.bars_due_at is not None:
        due = bars_due(universe, lake.get_asset_classes(universe), _utc(request.bars_due_at))
    runtime = build_tick_runtime(settings, universe, scoped=request.is_scoped, bars_due=due)
    try:
        return run_tick(
            state=state,
            lake=lake,
            registry=registry,
            settings=runtime.settings,
            as_of=request.as_of,
            dry_run=request.dry_run,
            notifier=runtime.notifier,
            broker_factory=runtime.broker_factory,
            plan=runtime.plan_for(state, dry_run=request.dry_run),
        )
    except BackdatedTickError as exc:
        raise ConflictError(str(exc)) from None


#: The tick's trading date as recorded on its portfolio snapshot (ticks that
#: traded); no-op and dry-run ticks fall back to the date in their id.
_AS_OF_SQL = (
    "(SELECT MAX(s.as_of) FROM portfolio_snapshots s WHERE s.tick_id = t.id) AS snapshot_as_of"
)


def _utc(when: datetime) -> datetime:
    """A naive time is read as UTC."""
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


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


def _row_to_view(row: Any, visible: set[str] | None) -> TickRunView:
    summary = row["summary_json"]
    if summary is not None:
        parsed = json.loads(summary)
        parsed.pop("owner", None)  # the running row's host and process (BE-44)
        summary = json.dumps(parsed) if parsed else None
    return TickRunView(
        id=row["id"],
        as_of=_as_of(row),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        status=row["status"],
        summary=None if summary is None else scope_summary(json.loads(summary), visible),
    )


def _order_books(state: Any, tick_id: str) -> list[str]:
    rows = state.sql(
        "SELECT DISTINCT portfolio_id FROM orders WHERE tick_id = ? ORDER BY portfolio_id",
        [tick_id],
    )
    return [r["portfolio_id"] or DEFAULT_PORTFOLIO_ID for r in rows]


#: Summary keys about the whole tick, which every reader sees (AS-02):
#: status and counts, the winner, the shadow outcomes, the quit rule, and
#: how the tick ran (a dry run or not, and whose money it traded).
GLOBAL_SUMMARY_KEYS = frozenset(
    {
        "reason",
        "error",
        "error_type",
        "winner_strategy_id",
        "exit_strategy_id",
        "winner_expected_return",
        "orders_placed",
        "fills",
        "shadow",
        "shadow_error",
        "quit_rule",
        "dry_run",
        "broker_mode",
    }
)


def scope_summary(summary: dict[str, Any], visible: set[str] | None) -> dict[str, Any]:
    """``summary`` as a reader who owns the portfolios ``visible`` may see
    it (``None``: a service, everything). A multi-book tick keeps only the
    reader's books under ``portfolios``. A single-book tick names its
    portfolio in ``portfolio_id`` (rows written before it: the default
    portfolio): its owner sees all of it, anyone else the global keys."""
    if visible is None:
        return summary
    shown = {k: v for k, v in summary.items() if k in GLOBAL_SUMMARY_KEYS}
    books = summary.get("portfolios")
    if isinstance(books, dict):
        shown["portfolios"] = {pid: v for pid, v in books.items() if pid in visible}
        return shown
    if summary.get("portfolio_id", DEFAULT_PORTFOLIO_ID) in visible:
        return summary
    return shown
