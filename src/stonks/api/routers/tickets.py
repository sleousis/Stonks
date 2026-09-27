"""Order tickets (roadmap 19.8): the orders a live book decided after the
close. Approve mode tickets, and the closes of a runaway run, wait for a
person. Approving needs a fresh second factor. Approved tickets go out in
the submit window before the next open."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query

from stonks.api.deps import PageDep, PrincipalDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.pagination import Page, page_of
from stonks.app.tickets import (
    TicketApproval,
    TicketList,
    TicketRejection,
    TicketStatusName,
    TicketSubmitResult,
    TicketSummary,
    TicketView,
)
from stonks.auth import Permission

router = APIRouter(prefix="/api/tickets", tags=["orders"], responses=PROBLEM_RESPONSES)

TicketId = Annotated[str, Path(max_length=64)]


@router.get("", response_model=Page[TicketView], operation_id="listTickets")
def list_tickets(
    services: ServicesDep,
    principal: PrincipalDep,
    page: PageDep,
    status: Annotated[TicketStatusName | None, Query()] = None,
    portfolio_id: Annotated[str | None, Query(max_length=64)] = None,
    tick_id: Annotated[str | None, Query(max_length=64)] = None,
) -> Page[TicketView]:
    """Your order tickets, newest first."""
    return page_of(
        services.tickets.list(principal, status=status, portfolio_id=portfolio_id, tick_id=tick_id),
        page,
    )


@router.get("/summary", response_model=TicketSummary, operation_id="getTicketSummary")
def ticket_summary(services: ServicesDep, principal: PrincipalDep) -> TicketSummary:
    """How many of your tickets wait for approval, per portfolio."""
    return services.tickets.summary(principal)


@router.post(
    "/approve",
    response_model=TicketList,
    operation_id="approveTickets",
    dependencies=needs(Permission.ORDER_APPROVE),
)
def approve_tickets(
    body: TicketApproval, services: ServicesDep, principal: PrincipalDep
) -> TicketList:
    """Approve tickets that wait for you, all or none. Needs a fresh second
    factor. Approved tickets are sent in their submit window."""
    return services.tickets.approve(principal, body)


@router.post(
    "/submit",
    response_model=TicketSubmitResult,
    operation_id="submitTickets",
    dependencies=needs(Permission.OPERATIONS_RUN),
)
def submit_tickets(services: ServicesDep, principal: PrincipalDep) -> TicketSubmitResult:
    """Send the approved tickets inside their window now (the live_submit
    job does this before each open)."""
    return services.tickets.submit_due(principal)


@router.get("/{ticket_id}", response_model=TicketView, operation_id="getTicket")
def get_ticket(ticket_id: TicketId, services: ServicesDep, principal: PrincipalDep) -> TicketView:
    return services.tickets.get(principal, ticket_id)


@router.post(
    "/{ticket_id}/reject",
    response_model=TicketView,
    operation_id="rejectTicket",
    dependencies=needs(Permission.PORTFOLIO_TRADE),
)
def reject_ticket(
    ticket_id: TicketId, body: TicketRejection, services: ServicesDep, principal: PrincipalDep
) -> TicketView:
    """Reject a ticket that waits for you, with a reason. Nothing is sent."""
    return services.tickets.reject(principal, ticket_id, body)
