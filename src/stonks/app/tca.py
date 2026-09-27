"""TcaService: transaction cost analysis and the trade journal for the CLI,
the API and MCP (BL-32, roadmap 9.3.4). The math lives in
:mod:`stonks.production.tca`; this module scopes it to the caller.

Summaries and the journal read one portfolio, resolved by the caller
(``PortfolioService.resolve`` in the API). An order's detail and its notes
look the order up and check that the caller owns its portfolio: another
user's order reads as missing (404), never forbidden. Only a note's author
edits it.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from stonks.accounts import NotFound, Scope, owned_portfolio
from stonks.app.context import AppContext
from stonks.app.errors import NotFoundError, ValidationError
from stonks.app.pagination import Page
from stonks.auth.policy import Permission, require
from stonks.auth.principal import Principal
from stonks.production.tca import (
    GROUP_BYS,
    GroupBy,
    JournalEntry,
    JournalError,
    JournalNote,
    Shortfall,
    TcaGroup,
    add_note,
    journal,
    load_order_tca,
    summarize,
    update_note,
)
from stonks.store.state import SqliteState

#: The caller: a :class:`Principal` (API, MCP) or a bare :class:`Scope`
#: (the CLI and in-process services).
Who = Scope | Principal


def _scope(who: Who) -> Scope:
    return who.scope if isinstance(who, Principal) else who


# ---- requests and views ----------------------------------------------------------


class NoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field(min_length=1, max_length=4000)


class ShortfallView(BaseModel):
    """Implementation shortfall of one order. Costs are positive, in bps of
    the filled quantity's value at the decision price; ``null`` while an
    input is not known yet."""

    side: str
    ordered_quantity: float
    filled_quantity: float
    decision_price: float
    #: Market price when the order arrived, before costs.
    arrival_price: float | None
    #: Volume-weighted fill price.
    fill_price: float | None
    #: The next session's open (the price a backtest fills at).
    benchmark_price: float | None
    #: The next session's close.
    post_close_price: float | None
    #: The cost model's estimate at the decision.
    expected_bps: float | None
    delay_bps: float | None
    impact_bps: float | None
    fee_bps: float | None
    is_bps: float | None
    #: Cost of the unfilled part, in bps of its value.
    opportunity_bps: float | None
    #: How much more live paid than the backtest's next-open convention.
    convention_bps: float | None
    #: Shortfall plus opportunity cost, in bps of the whole order.
    total_bps: float | None
    is_cost: float | None
    opportunity_cost: float | None

    @classmethod
    def of(cls, s: Shortfall) -> ShortfallView:
        return cls(**s.as_dict())


class JournalNoteView(BaseModel):
    id: int
    order_client_id: str
    author: str
    note: str
    created_at: str
    updated_at: str

    @classmethod
    def of(cls, n: JournalNote) -> JournalNoteView:
        return cls(**n.__dict__)


class JournalEntryView(BaseModel):
    """One order: why it was placed, the signal context, the outcome and the
    notes people added."""

    client_id: str
    portfolio_id: str | None
    strategy_id: str | None
    ticker: str
    side: str
    quantity: float
    status: str
    status_reason: str | None
    created_at: str
    decided_at: str | None
    decision_price: float | None
    #: signal, exit_no_pick, risk_rule or manual; null for orders placed
    #: before TCA was recorded.
    trigger: str | None
    #: The decision context: score, rank, candidates, constructor, target weight.
    context: dict[str, Any] | None
    shortfall: ShortfallView | None
    #: Move in the trade's favour from the decision to the next close, bps.
    next_session_move_bps: float | None
    notes: list[JournalNoteView]

    @classmethod
    def of(cls, e: JournalEntry) -> JournalEntryView:
        return cls(
            client_id=e.client_id,
            portfolio_id=e.portfolio_id,
            strategy_id=e.strategy_id,
            ticker=e.ticker,
            side=e.side,
            quantity=e.quantity,
            status=e.status,
            status_reason=e.status_reason,
            created_at=e.created_at,
            decided_at=e.decided_at,
            decision_price=e.decision_price,
            trigger=e.trigger,
            context=dict(e.context) if e.context is not None else None,
            shortfall=ShortfallView.of(e.shortfall) if e.shortfall is not None else None,
            next_session_move_bps=e.next_session_move_bps,
            notes=[JournalNoteView.of(n) for n in e.notes],
        )


class TcaGroupView(BaseModel):
    """Costs of a group of orders, weighted by notional, in bps."""

    key: str
    orders: int
    filled_orders: int
    filled_notional: float
    is_cost: float
    is_bps: float | None
    delay_bps: float | None
    impact_bps: float | None
    fee_bps: float | None
    opportunity_cost: float
    opportunity_bps: float | None
    convention_bps: float | None
    #: The cost model's estimate, over the orders that recorded one.
    expected_bps: float | None
    #: Realised shortfall minus the estimate, over the same orders.
    model_gap_bps: float | None

    @classmethod
    def of(cls, g: TcaGroup) -> TcaGroupView:
        return cls(**g.as_dict())


class TcaSummaryView(BaseModel):
    portfolio_id: str
    by: GroupBy
    since: date | None
    until: date | None
    groups: list[TcaGroupView]


# ---- the service -----------------------------------------------------------------


class TcaService:
    def __init__(self, context: AppContext) -> None:
        self._context = context

    def summary(
        self,
        portfolio_id: str,
        *,
        by: GroupBy = "all",
        since: date | None = None,
        until: date | None = None,
        strategy_id: str | None = None,
        ticker: str | None = None,
    ) -> TcaSummaryView:
        """Shortfall of one portfolio's orders grouped ``by`` strategy,
        ticker, portfolio or period (day, ISO week, month)."""
        if by not in GROUP_BYS:
            raise ValidationError(f"by must be one of {', '.join(GROUP_BYS)}")
        with self._context.state() as state:
            rows = load_order_tca(
                state,
                portfolio_id,
                since=since,
                until=until,
                strategy_id=strategy_id,
                ticker=ticker,
            )
        return TcaSummaryView(
            portfolio_id=portfolio_id,
            by=by,
            since=since,
            until=until,
            groups=[TcaGroupView.of(g) for g in summarize(rows, by)],
        )

    def journal(
        self,
        portfolio_id: str,
        *,
        since: date | None = None,
        strategy_id: str | None = None,
        ticker: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Page[JournalEntryView]:
        """One portfolio's orders, newest first, with reason, context,
        outcome and notes."""
        with self._context.state() as state:
            entries, total = journal(
                state,
                portfolio_id,
                since=since,
                strategy_id=strategy_id,
                ticker=ticker,
                limit=limit,
                offset=offset,
            )
        return Page[JournalEntryView](
            items=[JournalEntryView.of(e) for e in entries],
            total=total,
            limit=limit,
            offset=offset,
        )

    def order(self, who: Who, client_id: str) -> JournalEntryView:
        """One order of the caller's, in full."""
        scope = _scope(who)
        if isinstance(who, Principal):
            require(who, Permission.READ)
        with self._context.state() as state:
            portfolio_id = self._order_portfolio(state, scope, client_id)
            entries, _ = journal(state, portfolio_id, client_id=client_id, limit=1)
        if not entries:
            raise NotFoundError(f"order {client_id!r} not found")
        return JournalEntryView.of(entries[0])

    def add_note(self, who: Who, client_id: str, request: NoteRequest) -> JournalNoteView:
        """Add a note to one of the caller's orders."""
        scope = _scope(who)
        if isinstance(who, Principal):
            require(who, Permission.PORTFOLIO_MANAGE)
        with self._context.state() as state:
            portfolio_id = self._order_portfolio(state, scope, client_id)
            try:
                note = add_note(
                    state, client_id, portfolio_id, author=scope.actor, note=request.note
                )
            except JournalError as exc:
                raise NotFoundError(str(exc)) from None
            except ValueError as exc:
                raise ValidationError(str(exc)) from None
        return JournalNoteView.of(note)

    def update_note(self, who: Who, note_id: int, request: NoteRequest) -> JournalNoteView:
        """Replace the text of a note the caller wrote."""
        scope = _scope(who)
        if isinstance(who, Principal):
            require(who, Permission.PORTFOLIO_MANAGE)
        with self._context.state() as state:
            rows = state.sql("SELECT portfolio_id FROM journal_notes WHERE id = ?", [note_id])
            if not rows:
                raise NotFoundError(f"note {note_id} not found")
            portfolio_id = self._owned(state, scope, rows[0]["portfolio_id"], f"note {note_id}")
            try:
                note = update_note(
                    state, note_id, portfolio_id, author=scope.actor, note=request.note
                )
            except JournalError as exc:
                raise NotFoundError(str(exc)) from None
            except ValueError as exc:
                raise ValidationError(str(exc)) from None
        return JournalNoteView.of(note)

    # ---- helpers -------------------------------------------------------------------

    def _order_portfolio(self, state: SqliteState, scope: Scope, client_id: str) -> str:
        rows = state.sql("SELECT portfolio_id FROM orders WHERE client_id = ?", [client_id])
        if not rows or rows[0]["portfolio_id"] is None:
            raise NotFoundError(f"order {client_id!r} not found")
        return self._owned(state, scope, rows[0]["portfolio_id"], f"order {client_id!r}")

    @staticmethod
    def _owned(state: SqliteState, scope: Scope, portfolio_id: str, what: str) -> str:
        try:
            return owned_portfolio(state, scope, portfolio_id).id
        except NotFound:
            raise NotFoundError(f"{what} not found") from None
