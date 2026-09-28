"""TicketService: the order tickets live books decided after the close
(roadmap 19.8, see :mod:`stonks.production.tickets`).

- Reading needs ``read`` and shows your own portfolios' tickets only.
  Another person's ticket reads as missing (404). MCP and API tokens can
  read them.
- Approving needs ``orders.approve``: a signed-in browser with a fresh
  second factor. One code covers every ticket approved while it is fresh,
  so a batch takes one code. Nothing is approved through MCP or an API
  token (403 ``step_up_required``).
- Rejecting needs ``portfolio.trade`` and a reason.
- Sending the approved tickets now (what the ``live_submit`` job does)
  needs ``operations.run``. It still only sends tickets inside their
  window.
- The CLI passes a bare :class:`Scope` instead of a principal: shell access
  already implies the machine. The role still has to allow the action, and
  approving needs :data:`APPROVE_PHRASE` typed at a terminal, the shell's
  stand-in for a fresh second factor (like ``stonks halts resume``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from stonks.accounts import Scope
from stonks.app.context import AppContext
from stonks.app.errors import ConflictError, NotFoundError, ValidationError
from stonks.auth.errors import PermissionDenied
from stonks.auth.policy import POLICY, Permission, require
from stonks.auth.principal import Principal
from stonks.production.tickets import (
    Ticket,
    TicketNotFound,
    TicketRefused,
    awaiting_counts,
    decide_tickets,
    get_ticket,
    list_tickets,
)
from stonks.store.state import SqliteState

TicketStatusName = Literal[
    "awaiting_approval",
    "approved",
    "rejected",
    "expired",
    "submitted",
    "filled",
    "unfilled",
    "cancelled",
    "failed",
]
_ID = r"^tkt_[a-f0-9]{8,32}$"

#: What the CLI asks you to type before it approves tickets.
APPROVE_PHRASE = "APPROVE TICKETS"

#: The caller: a :class:`Principal` (API, MCP) or a bare :class:`Scope` (the CLI).
Who = Principal | Scope


def _allow(who: Who, permission: Permission) -> None:
    """A principal goes through the policy, step-up included. A bare scope
    (the CLI) needs a role the policy allows for ``permission``."""
    if isinstance(who, Principal):
        require(who, permission)
    elif who.role not in POLICY[permission].roles:
        raise PermissionDenied(f"{permission.value} is not allowed for this user")


def _scope(who: Who) -> Scope:
    return who.scope if isinstance(who, Principal) else who


class TicketView(BaseModel):
    """One order ticket: the order a live book decided, why, and what
    became of it."""

    id: str
    portfolio_id: str
    portfolio_name: str
    tick_id: str | None
    as_of: date = Field(description="The day the book decided.")
    client_id: str = Field(description="The order the ticket becomes at the broker.")
    strategy_id: str | None
    ticker: str
    side: Literal["buy", "sell"]
    quantity: float
    order_type: str
    limit_price: float | None
    position_effect: Literal["open", "close"] | None
    reference_price: float | None = Field(description="The price the book decided at.")
    notional: float | None = Field(description="quantity x (limit, else the reference price).")
    what_if: dict[str, Any] | None = Field(
        description="The broker's preview: commission and margin (when it has one)."
    )
    reason: dict[str, Any] = Field(description="Signal score, rank, target weight, trigger.")
    rules: list[dict[str, Any]] = Field(description="The risk and account rules that touched it.")
    hold: Literal["approve_mode", "runaway", "hard_to_borrow", "options"] | None = Field(
        description=(
            "Why it waits for a person (approve mode, a runaway run, a hard to borrow"
            " short sale, or a live option order)."
        )
    )
    status: TicketStatusName
    submit_after: datetime
    expires_at: datetime = Field(description="The submit deadline: unsent, it expires.")
    decided_by: str | None
    decided_at: datetime | None
    decision_reason: str | None
    submitted_at: datetime | None
    status_reason: str | None
    created_at: datetime


class TicketList(BaseModel):
    items: list[TicketView]


class TicketApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket_ids: list[Annotated[str, Field(pattern=_ID)]] = Field(min_length=1, max_length=200)


class TicketRejection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=500)


class TicketSummary(BaseModel):
    """Tickets waiting for you, for the badge in the menu."""

    awaiting_approval: int
    by_portfolio: dict[str, int]


class PortfolioSubmitView(BaseModel):
    portfolio_id: str
    status: Literal["ok", "partial", "skipped", "error"]
    sent: int
    failed: int
    held: int
    reason: str | None


class TicketSubmitResult(BaseModel):
    sent: int
    failed: int
    expired: int
    settled: int
    portfolios: list[PortfolioSubmitView]


def _view(t: Ticket, names: dict[str, str]) -> TicketView:
    reason = dict(t.reason)
    rules = list(reason.pop("rules", []) or [])
    effect = reason.get("position_effect")
    return TicketView(
        id=t.id,
        portfolio_id=t.portfolio_id,
        portfolio_name=names.get(t.portfolio_id, t.portfolio_id),
        tick_id=t.tick_id,
        as_of=t.as_of,
        client_id=t.client_id,
        strategy_id=t.strategy_id,
        ticker=t.ticker,
        side=t.side,  # type: ignore[arg-type]
        quantity=t.quantity,
        order_type=t.order.order_type,
        limit_price=t.limit_price,
        position_effect=effect if effect in ("open", "close") else None,
        reference_price=t.preview.get("reference_price"),
        notional=t.preview.get("notional"),
        what_if=t.preview.get("what_if"),
        reason=reason,
        rules=rules,
        hold=t.hold,
        status=t.status,
        submit_after=t.submit_after,
        expires_at=t.expires_at,
        decided_by=t.decided_by,
        decided_at=t.decided_at,
        decision_reason=t.decision_reason,
        submitted_at=t.submitted_at,
        status_reason=t.status_reason,
        created_at=t.created_at,
    )


@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except TicketNotFound as exc:
        raise NotFoundError(str(exc)) from None
    except TicketRefused as exc:
        raise ConflictError(str(exc)) from None


class TicketService:
    def __init__(self, context: AppContext, *, clock: Callable[[], datetime] | None = None) -> None:
        self._ctx = context
        self._clock = clock or (lambda: datetime.now(UTC))

    # ---- reads ---------------------------------------------------------------------

    def list(
        self,
        principal: Who,
        *,
        status: str | None = None,
        portfolio_id: str | None = None,
        tick_id: str | None = None,
    ) -> list[TicketView]:
        """Your tickets, newest first."""
        _allow(principal, Permission.READ)
        with self._ctx.state() as state:
            names = self._portfolios(state, principal)
            ids = list(names) if portfolio_id is None else [portfolio_id]
            if portfolio_id is not None and portfolio_id not in names:
                raise NotFoundError(f"portfolio {portfolio_id!r} not found")
            tickets = list_tickets(state, portfolio_ids=ids, status=status, tick_id=tick_id)
            return [_view(t, names) for t in tickets]

    def get(self, principal: Who, ticket_id: str) -> TicketView:
        _allow(principal, Permission.READ)
        with self._ctx.state() as state, _errors():
            names = self._portfolios(state, principal)
            ticket = get_ticket(state, ticket_id)
            if ticket.portfolio_id not in names:
                raise TicketNotFound(f"ticket {ticket_id!r} not found")
            return _view(ticket, names)

    def summary(self, principal: Principal) -> TicketSummary:
        require(principal, Permission.READ)
        with self._ctx.state() as state:
            owner = None if principal.scope.is_service else principal.user_id
            counts = awaiting_counts(state, owner_id=owner)
        return TicketSummary(awaiting_approval=sum(counts.values()), by_portfolio=counts)

    # ---- decisions -----------------------------------------------------------------

    def approve(
        self, principal: Who, body: TicketApproval, *, confirmation: str | None = None
    ) -> TicketList:
        """Approve tickets that wait for you, all or none (a fresh second
        factor). An approved ticket is sent in its submit window. The CLI
        (a bare scope) sends ``confirmation``, which must be
        :data:`APPROVE_PHRASE`."""
        _allow(principal, Permission.ORDER_APPROVE)
        if isinstance(principal, Scope) and (confirmation or "").strip() != APPROVE_PHRASE:
            raise ValidationError(f"type {APPROVE_PHRASE!r} to approve tickets")
        return self._decide(principal, body.ticket_ids, approve=True, reason=None)

    def reject(self, principal: Who, ticket_id: str, body: TicketRejection) -> TicketView:
        """Reject a ticket that waits for you, with a reason. Nothing is sent."""
        _allow(principal, Permission.PORTFOLIO_TRADE)
        [view] = self._decide(principal, [ticket_id], approve=False, reason=body.reason).items
        return view

    def _decide(
        self, principal: Who, ids: list[str], *, approve: bool, reason: str | None
    ) -> TicketList:
        with self._ctx.state() as state, _errors():
            names = self._portfolios(state, principal)
            tickets = decide_tickets(
                state,
                ids,
                approve=approve,
                actor=principal.actor,
                reason=reason,
                now=self._clock(),
                portfolio_ids=list(names),
            )
            return TicketList(items=[_view(t, names) for t in tickets])

    # ---- the submit job --------------------------------------------------------------

    def submit_due(self, principal: Principal) -> TicketSubmitResult:
        """Send every approved ticket inside its window now (the
        ``live_submit`` job's work, for an operator)."""
        require(principal, Permission.OPERATIONS_RUN)
        from stonks.production.settings_builder import submit_broker_opener
        from stonks.production.submit import submit_tickets

        with self._ctx.state() as state:
            settings = self._ctx.settings
            result = submit_tickets(
                state,
                submit_broker_opener(settings, state),
                risk=settings.production.risk,
                live=settings.production.live,
            )
        return TicketSubmitResult(
            sent=result.sent,
            failed=result.failed,
            expired=len(result.expired),
            settled=result.settled,
            portfolios=[
                PortfolioSubmitView(
                    portfolio_id=p.portfolio_id,
                    status=p.status,
                    sent=p.sent,
                    failed=p.failed,
                    held=p.held,
                    reason=p.reason,
                )
                for p in result.portfolios
            ],
        )

    # ---- helpers -------------------------------------------------------------------

    @staticmethod
    def _portfolios(state: SqliteState, principal: Who) -> dict[str, str]:
        """The portfolios whose tickets the caller may see, with names."""
        scope = _scope(principal)
        if scope.is_service:
            rows = state.sql("SELECT id, name FROM portfolios")
        else:
            rows = state.sql("SELECT id, name FROM portfolios WHERE owner_id = ?", [scope.user_id])
        return {r["id"]: r["name"] for r in rows}
