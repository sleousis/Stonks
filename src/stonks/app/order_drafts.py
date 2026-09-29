"""OrderDraftService: orders proposed now, approved by a person later
(roadmap 20.4 safety, see :mod:`stonks.production.order_drafts`).

- Creating a draft needs ``portfolio.trade`` on one of your portfolios. The
  assistant creates them (source ``assistant``) inside its envelope: only
  when order tools are on, the person is not frozen and no kill switch
  covers them, with its ticker allowlist and notional caps. The console and
  MCP may create them too (price band and expiry only).
- Approving one needs ``orders.approve``: a signed-in browser with a fresh
  second factor. Nothing is approved through the assistant, Telegram, MCP
  or an API token. Approval places the draft as a manual order through
  every check (:class:`~stonks.app.manual_orders.ManualOrdersService`). A
  refused order leaves the draft rejected with the reason.
- Rejecting one needs ``portfolio.trade``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from stonks.app.context import AppContext
from stonks.app.errors import ConflictError, NotFoundError, ValidationError
from stonks.app.manual_orders import (
    ManualOrderRequest,
    ManualOrderResult,
    ManualOrdersService,
    OrderRefusedError,
)
from stonks.assistant.guard import gate_for
from stonks.auth.errors import PermissionDenied
from stonks.auth.policy import Permission, require
from stonks.auth.principal import Principal
from stonks.production.order_drafts import (
    Draft,
    DraftNotFound,
    DraftRefused,
    DraftRequest,
    DraftSource,
    Envelope,
    claim_draft,
    create_draft,
    get_draft,
    list_drafts,
    mark_placed,
)

_KEY = r"^[A-Za-z0-9_.:\-]{1,120}$"
_TICKER = r"^[A-Za-z0-9][A-Za-z0-9._\-^=]{0,31}$"


class OrderDraftCreate(BaseModel):
    """An order to propose. The server prices it and checks it; a person
    approves it in the web app before anything is placed."""

    model_config = ConfigDict(extra="forbid")

    portfolio_id: str | None = Field(default=None, max_length=64)
    ticker: str = Field(pattern=_TICKER)
    side: Literal["buy", "sell"]
    quantity: float = Field(gt=0, le=1e9)
    order_type: Literal["market", "limit"] = "market"
    limit_price: float | None = Field(default=None, gt=0)
    reason: str = Field(min_length=3, max_length=500)
    retry_key: str = Field(
        pattern=_KEY, description="The same key returns the draft already made (a safe retry)."
    )


class OrderDraftDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=500)


class OrderDraftView(BaseModel):
    id: str
    portfolio_id: str
    source: Literal["assistant", "console", "mcp"]
    conversation_id: str | None
    ticker: str
    side: Literal["buy", "sell"]
    quantity: float
    order_type: Literal["market", "limit"]
    limit_price: float | None
    reason: str
    reference_price: float = Field(description="The latest close, computed by the server.")
    notional: float = Field(description="quantity x reference_price, computed by the server.")
    status: Literal["pending", "placed", "rejected", "expired", "cancelled"]
    client_id: str | None = Field(description="The manual order it became, once placed.")
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None
    decided_by: str | None
    decision_note: str | None


class OrderDraftApproval(BaseModel):
    draft: OrderDraftView
    order: ManualOrderResult


def draft_view(d: Draft) -> OrderDraftView:
    return OrderDraftView(
        id=d.id,
        portfolio_id=d.portfolio_id,
        source=d.source,  # type: ignore[arg-type]
        conversation_id=d.conversation_id,
        ticker=d.ticker,
        side=d.side,  # type: ignore[arg-type]
        quantity=d.quantity,
        order_type=d.order_type,  # type: ignore[arg-type]
        limit_price=d.limit_price,
        reason=d.reason,
        reference_price=d.reference_price,
        notional=d.notional,
        status=d.status,  # type: ignore[arg-type]
        client_id=d.client_id,
        created_at=datetime.fromisoformat(d.created_at),
        expires_at=datetime.fromisoformat(d.expires_at),
        decided_at=datetime.fromisoformat(d.decided_at) if d.decided_at else None,
        decided_by=d.decided_by,
        decision_note=d.decision_note,
    )


def _source(principal: Principal) -> DraftSource:
    if principal.via == "assistant":
        return "assistant"
    if principal.via in ("token", "legacy"):
        return "mcp"
    return "console"


class OrderDraftService:
    def __init__(
        self,
        context: AppContext,
        manual_orders: ManualOrdersService,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._ctx = context
        self._manual = manual_orders
        self._clock = clock or (lambda: datetime.now(UTC))

    def create(
        self, principal: Principal, body: OrderDraftCreate, *, conversation_id: str | None = None
    ) -> OrderDraftView:
        require(principal, Permission.PORTFOLIO_TRADE)
        source = _source(principal)
        now = self._clock()
        with self._ctx.state() as state:
            pid = self._portfolio(principal, body.portfolio_id)
            envelope = self._envelope(source)
            if source == "assistant":
                gate = gate_for(
                    state, principal.user_id, self._ctx.settings.assistant.envelope, now=now
                )
                if not gate.order_tools:
                    raise PermissionDenied(
                        "the assistant may not draft orders now"
                        + (f": {gate.reason}" if gate.reason else " (research only)")
                    )
            with self._ctx.lake() as lake, state.transaction():
                try:
                    draft, _ = create_draft(
                        state,
                        lake,
                        DraftRequest(
                            owner_id=principal.user_id,
                            portfolio_id=pid,
                            source=source,
                            retry_key=body.retry_key,
                            ticker=body.ticker,
                            side=body.side,
                            quantity=body.quantity,
                            reason=body.reason,
                            order_type=body.order_type,
                            limit_price=body.limit_price,
                            conversation_id=conversation_id,
                        ),
                        envelope,
                        now=now,
                    )
                except DraftRefused as exc:
                    raise ConflictError(str(exc)) from None
        return draft_view(draft)

    def list(self, principal: Principal, *, status: str | None = None) -> list[OrderDraftView]:
        require(principal, Permission.READ)
        with self._ctx.state() as state:
            return [
                draft_view(d)
                for d in list_drafts(state, principal.user_id, status=status, now=self._clock())
            ]

    def approve(self, principal: Principal, draft_id: str) -> OrderDraftApproval:
        """Place a pending draft as a manual order (a fresh second factor)."""
        require(principal, Permission.ORDER_APPROVE)
        now = self._clock()
        with self._ctx.state() as state:
            try:
                draft = claim_draft(
                    state, principal.user_id, draft_id, "placed", actor=principal.actor, now=now
                )
            except DraftNotFound as exc:
                raise NotFoundError(str(exc)) from None
            except DraftRefused as exc:
                raise ConflictError(str(exc)) from None
        request = ManualOrderRequest(
            portfolio_id=draft.portfolio_id,
            ticker=draft.ticker,
            side=draft.side,  # type: ignore[arg-type]
            quantity=draft.quantity,
            order_type=draft.order_type,  # type: ignore[arg-type]
            limit_price=draft.limit_price,
            reason=f"draft {draft.id}: {draft.reason}"[:500],
            client_id=draft.id,
        )
        try:
            order = self._manual.place(principal, request)
        except Exception as exc:
            with self._ctx.state() as state:
                state.execute(
                    "UPDATE order_drafts SET status = 'rejected', decision_note = ? WHERE id = ?",
                    [f"not placed: {exc}"[:500], draft.id],
                )
            if isinstance(exc, OrderRefusedError | PermissionDenied | NotFoundError):
                raise
            raise ConflictError(f"the draft was not placed: {exc}") from None
        with self._ctx.state() as state:
            mark_placed(state, draft.id, order.client_id)
            if order.status == "rejected":
                # the order exists but never traded: the draft says so
                state.execute(
                    "UPDATE order_drafts SET status = 'rejected', decision_note = ? WHERE id = ?",
                    [f"order rejected: {order.reason or 'no reason given'}"[:500], draft.id],
                )
            placed = get_draft(state, principal.user_id, draft.id)
        return OrderDraftApproval(draft=draft_view(placed), order=order)

    def reject(
        self, principal: Principal, draft_id: str, body: OrderDraftDecision
    ) -> OrderDraftView:
        require(principal, Permission.PORTFOLIO_TRADE)
        with self._ctx.state() as state:
            try:
                draft = claim_draft(
                    state,
                    principal.user_id,
                    draft_id,
                    "rejected",
                    actor=principal.actor,
                    note=body.note,
                    now=self._clock(),
                )
            except DraftNotFound as exc:
                raise NotFoundError(str(exc)) from None
            except DraftRefused as exc:
                raise ConflictError(str(exc)) from None
        return draft_view(draft)

    # ---- helpers -----------------------------------------------------------------

    def _portfolio(self, principal: Principal, portfolio_id: str | None) -> str:
        from stonks.app.portfolio import PortfolioService

        pid = PortfolioService(self._ctx).resolve(principal, portfolio_id)
        with self._ctx.state() as state:
            rows = state.sql("SELECT status FROM portfolios WHERE id = ?", [pid])
        if not rows or rows[0]["status"] != "active":
            raise ValidationError(f"portfolio {pid} is not active")
        return pid

    def _envelope(self, source: DraftSource) -> Envelope:
        settings = self._ctx.settings
        env = settings.assistant.envelope
        base = Envelope(
            price_band=env.price_band,
            ttl=timedelta(minutes=env.draft_ttl_minutes),
            max_price_staleness_days=settings.production.max_price_staleness_days,
        )
        if source != "assistant":
            return base
        return Envelope(
            allowed_tickers=(
                frozenset(t.upper() for t in env.allowed_tickers)
                if env.allowed_tickers is not None
                else None
            ),
            max_order_notional=env.max_order_notional,
            max_day_notional=env.max_day_notional,
            price_band=base.price_band,
            ttl=base.ttl,
            max_price_staleness_days=base.max_price_staleness_days,
        )
