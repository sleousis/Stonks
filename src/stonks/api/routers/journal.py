"""The round-trip journal (roadmap 23.3).

Reads are scoped to the caller: every route reads one of your portfolios
(``portfolio_id``, default your own book), and a trade of another user's
portfolio is a 404. Reviewing a trade and editing playbooks needs
``portfolio.manage``. Playbooks are yours alone."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query

from stonks.api.deps import PageDep, PortfolioIdDep, PrincipalDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.app.journal import (
    AnnotationRequest,
    AnnotationView,
    BreakdownBy,
    BreakdownView,
    JournalLabelsView,
    JournalService,
    JournalTradeDetailView,
    JournalTradeView,
    Origin,
    PlanFilter,
    PlaybookCreate,
    PlaybookUpdate,
    PlaybookView,
    PnlCalendarView,
    TradeFilter,
    TradeStatus,
)
from stonks.app.pagination import Page
from stonks.auth import Permission

router = APIRouter(prefix="/api/journal", tags=["journal"], responses=PROBLEM_RESPONSES)


def get_journal(services: ServicesDep) -> JournalService:
    return JournalService(services.context)


JournalDep = Annotated[JournalService, Depends(get_journal)]
TradeId = Annotated[int, Path(ge=1, description="the id of the fill that opened the trade")]
PlaybookId = Annotated[str, Path(max_length=64)]
Since = Annotated[date | None, Query(description="closed on or after this day (open: entered)")]
Until = Annotated[date | None, Query(description="closed on or before this day (open: entered)")]
Sleeve = Annotated[str | None, Query(max_length=128, description="one strategy id, or manual")]
OriginQ = Annotated[Origin | None, Query(description="strategy or manual orders")]


@router.get("/trades", response_model=Page[JournalTradeView], operation_id="listJournalTrades")
def list_journal_trades(
    journal: JournalDep,
    page: PageDep,
    portfolio_id: PortfolioIdDep,
    status: Annotated[TradeStatus, Query(description="all, open or closed")] = "all",
    sleeve: Sleeve = None,
    origin: OriginQ = None,
    ticker: Annotated[str | None, Query(max_length=64)] = None,
    since: Since = None,
    until: Until = None,
    tag: Annotated[str | None, Query(max_length=40)] = None,
    mistake: Annotated[str | None, Query(max_length=40)] = None,
    playbook_id: Annotated[str | None, Query(max_length=64)] = None,
    plan: Annotated[PlanFilter | None, Query(description="followed, broke or not_said")] = None,
) -> Page[JournalTradeView]:
    """Your round trips built from fills, open first then newest exit
    first: P&L, holding time, excursions, R multiple and exit efficiency,
    with your tags, mistakes and playbook."""
    filters = TradeFilter(
        status=status,
        sleeve=sleeve,
        origin=origin,
        ticker=ticker,
        since=since,
        until=until,
        tag=tag,
        mistake=mistake,
        playbook_id=playbook_id,
        plan=plan,
    )
    return journal.trades(portfolio_id, filters, limit=page.limit, offset=page.offset)


@router.get(
    "/trades/{trade_id}",
    response_model=JournalTradeDetailView,
    operation_id="getJournalTrade",
)
def get_journal_trade(
    trade_id: TradeId, journal: JournalDep, portfolio_id: PortfolioIdDep
) -> JournalTradeDetailView:
    """One trade with every leg and its review."""
    return journal.trade(portfolio_id, trade_id)


@router.put(
    "/trades/{trade_id}/review",
    response_model=AnnotationView,
    operation_id="reviewJournalTrade",
    dependencies=needs(Permission.PORTFOLIO_MANAGE),
)
def review_journal_trade(
    trade_id: TradeId,
    body: AnnotationRequest,
    journal: JournalDep,
    portfolio_id: PortfolioIdDep,
    principal: PrincipalDep,
) -> AnnotationView:
    """Set a trade's tags, mistakes, playbook, whether you followed the
    plan, and a short review. Replaces what was there."""
    return journal.annotate(principal, portfolio_id, trade_id, body)


@router.get("/calendar", response_model=PnlCalendarView, operation_id="getPnlCalendar")
def get_pnl_calendar(
    journal: JournalDep,
    portfolio_id: PortfolioIdDep,
    since: Since = None,
    until: Until = None,
    sleeve: Sleeve = None,
    origin: OriginQ = None,
) -> PnlCalendarView:
    """Realised P&L by exit day with weekly and monthly totals, in the
    portfolio's base currency."""
    return journal.calendar(portfolio_id, since=since, until=until, sleeve=sleeve, origin=origin)


@router.get("/breakdown", response_model=BreakdownView, operation_id="getJournalBreakdown")
def get_journal_breakdown(
    journal: JournalDep,
    portfolio_id: PortfolioIdDep,
    by: Annotated[
        BreakdownBy,
        Query(
            description="sleeve, origin, ticker, side, exit_trigger, tag, mistake, playbook, plan"
        ),
    ] = "all",
    since: Since = None,
    until: Until = None,
    sleeve: Sleeve = None,
    origin: OriginQ = None,
) -> BreakdownView:
    """Win rate, P&L, profit factor, average R and exit efficiency per
    group of trades."""
    return journal.breakdown(
        portfolio_id, by=by, since=since, until=until, sleeve=sleeve, origin=origin
    )


@router.get("/labels", response_model=JournalLabelsView, operation_id="listJournalLabels")
def list_journal_labels(journal: JournalDep, portfolio_id: PortfolioIdDep) -> JournalLabelsView:
    """The tags and mistakes you already used in this portfolio."""
    return journal.labels(portfolio_id)


@router.get("/playbooks", response_model=list[PlaybookView], operation_id="listPlaybooks")
def list_playbooks(
    journal: JournalDep,
    principal: PrincipalDep,
    include_archived: bool = False,
) -> list[PlaybookView]:
    """Your playbooks: the setups you trade, with their rules."""
    return journal.playbooks(principal, include_archived=include_archived)


@router.post(
    "/playbooks",
    status_code=201,
    response_model=PlaybookView,
    operation_id="createPlaybook",
    dependencies=needs(Permission.PORTFOLIO_MANAGE),
)
def create_playbook(
    body: PlaybookCreate, journal: JournalDep, principal: PrincipalDep
) -> PlaybookView:
    """Add a playbook."""
    return journal.create_playbook(principal, body)


@router.patch(
    "/playbooks/{playbook_id}",
    response_model=PlaybookView,
    operation_id="updatePlaybook",
    dependencies=needs(Permission.PORTFOLIO_MANAGE),
)
def update_playbook(
    playbook_id: PlaybookId, body: PlaybookUpdate, journal: JournalDep, principal: PrincipalDep
) -> PlaybookView:
    """Rename a playbook, change its rules, or archive it."""
    return journal.update_playbook(principal, playbook_id, body)
