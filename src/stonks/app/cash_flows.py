"""CashFlowService: deposits and withdrawals of your portfolios (roadmap 20.5).

Returns are time and money weighted from these flows
(:mod:`stonks.insights.returns`), so a deposit is never profit.

- Recording one moves the simulated book's cash: the flow row and a new
  ledger snapshot (same positions, cash adjusted, valued at the latest
  closes) are written in one transaction, with an ``audit_log`` row.
- A withdrawal larger than the cash, a date before the book's latest
  snapshot or after today, and a running tick are refused (409).
- A broker book's flows come from its sync, so recording is refused there.
- Another person's portfolio is a 404, admins included.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from stonks.accounts import NotFound, Role, Scope, owned_portfolio
from stonks.accounts import Portfolio as AccountPortfolio
from stonks.accounts.audit import AuditLog, iso_now
from stonks.app.context import AppContext
from stonks.app.errors import ConflictError, NotFoundError
from stonks.auth.errors import PermissionDenied
from stonks.auth.policy import Permission, require
from stonks.auth.principal import Principal
from stonks.insights.flows import RecordedFlow, recorded_flows
from stonks.production.prices import load_prices
from stonks.production.tick import _load_or_seed_portfolio, _snapshot_portfolio
from stonks.store.state import SqliteState

#: The caller: a :class:`Principal` (API) or a bare :class:`Scope` (CLI).
Who = Scope | Principal

#: Holdings are valued at their latest close, however old.
_ANY_AGE_DAYS = 36_500


class CashFlowCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["deposit", "withdrawal"]
    amount: float = Field(gt=0, le=1e12)
    flow_date: date | None = Field(default=None, description="Default: today (UTC).")
    note: str | None = Field(default=None, max_length=200)


class CashFlowView(BaseModel):
    id: int | None = Field(description="The recorded row; null for a flow from a broker sync.")
    portfolio_id: str
    flow_date: date
    kind: Literal["deposit", "withdrawal"]
    amount: float
    source: Literal["manual", "broker"]
    note: str | None = None


def _view(portfolio_id: str, f: RecordedFlow) -> CashFlowView:
    return CashFlowView(
        id=f.id,
        portfolio_id=portfolio_id,
        flow_date=f.day,
        kind=f.kind,  # type: ignore[arg-type]
        amount=f.amount,
        source=f.source,  # type: ignore[arg-type]
        note=f.note,
    )


def _scope(who: Who) -> Scope:
    return who.scope if isinstance(who, Principal) else who


class CashFlowService:
    def __init__(self, context: AppContext, *, clock: Callable[[], datetime] | None = None) -> None:
        self._ctx = context
        self._clock = clock or (lambda: datetime.now(UTC))

    def list(self, who: Who, portfolio_id: str) -> list[CashFlowView]:
        """Every deposit and withdrawal of one of your portfolios, oldest first."""
        if isinstance(who, Principal):
            require(who, Permission.READ)
        with self._ctx.state() as state:
            self._owned(state, who, portfolio_id)
            return [_view(portfolio_id, f) for f in recorded_flows(state, portfolio_id)]

    def record(self, who: Who, portfolio_id: str, body: CashFlowCreate) -> CashFlowView:
        scope = _scope(who)
        if isinstance(who, Principal):
            require(who, Permission.PORTFOLIO_MANAGE)
        elif not (scope.is_service or Role(scope.role).can_trade):
            raise PermissionDenied("recording cash flows needs a role that can trade")
        today = self._clock().date()
        day = body.flow_date or today
        if day > today:
            raise ConflictError("a cash flow cannot be dated in the future")
        with self._ctx.state() as state:
            account = self._owned(state, who, portfolio_id)
            if account.kind == "broker":
                raise ConflictError(
                    "a broker portfolio's deposits and withdrawals come from its sync"
                )
            if state.sql("SELECT 1 FROM tick_runs WHERE status = 'running' LIMIT 1"):
                raise ConflictError("a tick is running now; try again when it has finished")
            latest = state.sql(
                "SELECT MAX(as_of) AS d FROM portfolio_snapshots WHERE portfolio_id = ?"
                " AND source = 'tick'",
                [portfolio_id],
            )[0]["d"]
            if latest and day < date.fromisoformat(latest):
                raise ConflictError(
                    f"the book has a snapshot on {latest}; a cash flow cannot be dated before it"
                )
            initial = (
                float(account.initial_cash)
                if account.initial_cash is not None
                else float(self._ctx.settings.production.initial_cash)
            )
            marker = _snapshot_marker(state, portfolio_id)
            book = _load_or_seed_portfolio(state, initial, portfolio_id)
            signed = body.amount if body.kind == "deposit" else -body.amount
            if book.cash + signed < -1e-9:
                raise ConflictError(
                    f"a withdrawal of {body.amount:g} is more than the cash ({book.cash:g})"
                )
            held = [t for t, q in book.positions.items() if abs(q) > 1e-12]
            with self._ctx.lake() as lake:
                prices = load_prices(lake, [], held, day, max_staleness_days=_ANY_AGE_DAYS).prices
            book.cash += signed
            with state.transaction():
                # The book was read outside this write lock: a tick or a
                # manual order may have written it since.
                if _snapshot_marker(state, portfolio_id) != marker or state.sql(
                    "SELECT 1 FROM tick_runs WHERE status = 'running' LIMIT 1"
                ):
                    raise ConflictError("the book changed while the flow was checked; try again")
                cur = state.execute(
                    "INSERT INTO portfolio_cash_flows (portfolio_id, flow_date, kind, amount,"
                    " note, created_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        portfolio_id,
                        day.isoformat(),
                        body.kind,
                        body.amount,
                        body.note,
                        iso_now(),
                        scope.actor,
                    ],
                )
                _snapshot_portfolio(state, None, book, prices, day, portfolio_id=portfolio_id)
                AuditLog(state).record(
                    scope.actor,
                    f"cash_flow.{body.kind}",
                    "portfolio",
                    portfolio_id,
                    portfolio_id=portfolio_id,
                    details={"amount": body.amount, "flow_date": day.isoformat()},
                )
            flow_id = int(cur.lastrowid or 0)
        return CashFlowView(
            id=flow_id,
            portfolio_id=portfolio_id,
            flow_date=day,
            kind=body.kind,
            amount=body.amount,
            source="manual",
            note=body.note,
        )

    @staticmethod
    def _owned(state: SqliteState, who: Who, portfolio_id: str) -> AccountPortfolio:
        try:
            return owned_portfolio(state, _scope(who), portfolio_id)
        except NotFound as exc:
            raise NotFoundError(str(exc)) from None


def _snapshot_marker(state: SqliteState, portfolio_id: str) -> int:
    """The newest snapshot id of the book (0 for none)."""
    row = state.sql(
        "SELECT COALESCE(MAX(id), 0) AS m FROM portfolio_snapshots WHERE portfolio_id = ?",
        [portfolio_id],
    )[0]
    return int(row["m"])
