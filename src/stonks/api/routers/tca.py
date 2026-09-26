"""Transaction cost analysis and the trade journal (BL-32, roadmap 9.3.4).

Reads are scoped to the caller: summaries and the journal read one of your
portfolios (``portfolio_id``, default your own book), and an order or note
of another user's portfolio is a 404. Adding and editing notes needs
``portfolio.manage``; only a note's author edits it."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query

from stonks.api.deps import PageDep, PortfolioIdDep, PrincipalDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.pagination import Page
from stonks.app.tca import (
    JournalEntryView,
    JournalNoteView,
    NoteRequest,
    TcaService,
    TcaSummaryView,
)
from stonks.auth import Permission
from stonks.production.tca import GroupBy

router = APIRouter(prefix="/api/tca", tags=["tca"], responses=PROBLEM_RESPONSES)


def get_tca(services: ServicesDep) -> TcaService:
    return TcaService(services.context)


TcaDep = Annotated[TcaService, Depends(get_tca)]
ClientId = Annotated[str, Path(max_length=200, description="the order's client id")]
Since = Annotated[date | None, Query(description="orders decided on or after this day")]


@router.get("/summary", response_model=TcaSummaryView, operation_id="getTcaSummary")
def tca_summary(
    tca: TcaDep,
    portfolio_id: PortfolioIdDep,
    by: Annotated[
        GroupBy, Query(description="group by strategy, ticker, portfolio, day, week or month")
    ] = "all",
    since: Since = None,
    until: Annotated[date | None, Query(description="orders decided on or before this day")] = None,
    strategy_id: str | None = None,
    ticker: str | None = None,
) -> TcaSummaryView:
    """Implementation shortfall (delay, impact, fees, opportunity) against
    the cost model's estimate, weighted by notional, in bps."""
    return tca.summary(
        portfolio_id, by=by, since=since, until=until, strategy_id=strategy_id, ticker=ticker
    )


@router.get("/journal", response_model=Page[JournalEntryView], operation_id="listJournal")
def list_journal(
    tca: TcaDep,
    page: PageDep,
    portfolio_id: PortfolioIdDep,
    since: Since = None,
    strategy_id: str | None = None,
    ticker: str | None = None,
) -> Page[JournalEntryView]:
    """The trade journal, newest first: each order with its reason, the
    signal context, the outcome and your notes."""
    return tca.journal(
        portfolio_id,
        since=since,
        strategy_id=strategy_id,
        ticker=ticker,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/orders/{client_id}", response_model=JournalEntryView, operation_id="getOrderTca")
def get_order_tca(client_id: ClientId, tca: TcaDep, principal: PrincipalDep) -> JournalEntryView:
    """One of your orders in full: decision, context, shortfall and notes."""
    return tca.order(principal, client_id)


@router.post(
    "/orders/{client_id}/notes",
    status_code=201,
    response_model=JournalNoteView,
    operation_id="addJournalNote",
    dependencies=needs(Permission.PORTFOLIO_MANAGE),
)
def add_journal_note(
    client_id: ClientId, body: NoteRequest, tca: TcaDep, principal: PrincipalDep
) -> JournalNoteView:
    """Add a free-text note to one of your orders."""
    return tca.add_note(principal, client_id, body)


@router.put(
    "/notes/{note_id}",
    response_model=JournalNoteView,
    operation_id="updateJournalNote",
    dependencies=needs(Permission.PORTFOLIO_MANAGE),
)
def update_journal_note(
    note_id: int, body: NoteRequest, tca: TcaDep, principal: PrincipalDep
) -> JournalNoteView:
    """Replace the text of a note you wrote."""
    return tca.update_note(principal, note_id, body)
