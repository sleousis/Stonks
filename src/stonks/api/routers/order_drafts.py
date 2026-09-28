"""Order drafts (roadmap 20.4): orders proposed (by the assistant, MCP or
the console), priced and checked by the server, and approved by a person
in the web app with a fresh second factor. Approval places a manual order
through every check."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query

from stonks.api.deps import PageDep, PrincipalDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.order_drafts import (
    OrderDraftApproval,
    OrderDraftCreate,
    OrderDraftDecision,
    OrderDraftView,
)
from stonks.app.pagination import Page, page_of
from stonks.auth import Permission

router = APIRouter(prefix="/api/orders/drafts", tags=["orders"], responses=PROBLEM_RESPONSES)

DraftId = Annotated[str, Path(max_length=64)]


@router.get("", response_model=Page[OrderDraftView], operation_id="listOrderDrafts")
def list_order_drafts(
    services: ServicesDep,
    principal: PrincipalDep,
    page: PageDep,
    status: Annotated[
        Literal["pending", "placed", "rejected", "expired", "cancelled"] | None, Query()
    ] = None,
) -> Page[OrderDraftView]:
    """Your order drafts, newest first."""
    return page_of(services.order_drafts.list(principal, status=status), page)


@router.post(
    "",
    status_code=201,
    response_model=OrderDraftView,
    operation_id="createOrderDraft",
    dependencies=needs(Permission.PORTFOLIO_TRADE),
)
def create_order_draft(
    body: OrderDraftCreate, services: ServicesDep, principal: PrincipalDep
) -> OrderDraftView:
    """Propose an order. The server prices it at the latest close, checks
    the price band and caps, and keeps it until you approve, reject or it
    expires. The same retry_key returns the draft already made."""
    return services.order_drafts.create(principal, body)


@router.post(
    "/{draft_id}/approve",
    response_model=OrderDraftApproval,
    operation_id="approveOrderDraft",
    dependencies=needs(Permission.ORDER_APPROVE),
)
def approve_order_draft(
    draft_id: DraftId, services: ServicesDep, principal: PrincipalDep
) -> OrderDraftApproval:
    """Place a pending draft as a manual order, through the kill switch,
    every halt and every risk rule. Needs a fresh second factor."""
    return services.order_drafts.approve(principal, draft_id)


@router.post(
    "/{draft_id}/reject",
    response_model=OrderDraftView,
    operation_id="rejectOrderDraft",
    dependencies=needs(Permission.PORTFOLIO_TRADE),
)
def reject_order_draft(
    draft_id: DraftId,
    body: OrderDraftDecision,
    services: ServicesDep,
    principal: PrincipalDep,
) -> OrderDraftView:
    return services.order_drafts.reject(principal, draft_id, body)
