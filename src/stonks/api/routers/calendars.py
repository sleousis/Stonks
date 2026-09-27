"""Event calendars and news (roadmap 20.7): earnings, ex-dividend dates and
economic releases for your holdings, a watchlist, some tickers or all,
the news and sentiment of those tickers, and the order ticket's
earnings check."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from stonks.api.deps import OptionalPrincipalDep, PrincipalDep, ServicesDep, needs
from stonks.api.errors import PROBLEM_RESPONSES
from stonks.api.routers._jobs_common import JOB_CREATED, accepted
from stonks.app.calendars import (
    CALENDAR_REFRESH_JOB,
    CalendarFilter,
    CalendarRefreshRequest,
    CalendarRefreshView,
    CalendarScope,
    CalendarView,
    EarningsWarningsView,
    NewsView,
)
from stonks.app.errors import ValidationError
from stonks.app.jobs import Job
from stonks.auth import Permission
from stonks.calendars.alerts import alert_kinds

router = APIRouter(prefix="/api/calendars", tags=["calendars"], responses=PROBLEM_RESPONSES)

TickersQuery = Annotated[
    str | None, Query(max_length=8000, description="comma-separated instrument ids")
]
ScopeQuery = Annotated[
    CalendarScope,
    Query(description="holdings (default), watchlists, tickers or all"),
]
WatchlistQuery = Annotated[
    str | None, Query(max_length=64, description="one of your watchlists (scope watchlists)")
]
PortfolioQuery = Annotated[
    str | None, Query(max_length=64, description="one of your portfolios (scope holdings)")
]


def _split(value: str | None) -> list[str]:
    return [t for t in (value or "").replace(" ", ",").split(",") if t]


def _filter(
    scope: CalendarScope,
    watchlist_id: str | None,
    portfolio_id: str | None,
    tickers: str | None,
) -> CalendarFilter:
    try:
        return CalendarFilter(
            scope=scope,
            watchlist_id=watchlist_id,
            portfolio_id=portfolio_id,
            tickers=_split(tickers),
        )
    except PydanticValidationError as exc:
        raise ValidationError(exc.errors()[0]["msg"]) from None


@router.get("", response_model=CalendarView, operation_id="getCalendar")
def get_calendar(
    services: ServicesDep,
    principal: PrincipalDep,
    scope: ScopeQuery = "holdings",
    watchlist_id: WatchlistQuery = None,
    portfolio_id: PortfolioQuery = None,
    tickers: TickersQuery = None,
    start: date | None = None,
    end: date | None = None,
    countries: Annotated[
        str | None, Query(max_length=400, description="economic events of these ISO alpha-2 codes")
    ] = None,
) -> CalendarView:
    """Earnings, ex-dividend dates and economic releases from ``start``
    (default today) to ``end`` (default two weeks on, at most 120 days)."""
    return services.calendars.calendar(
        principal,
        _filter(scope, watchlist_id, portfolio_id, tickers),
        start=start,
        end=end,
        countries=[c.upper() for c in _split(countries)] or None,
    )


@router.get("/news", response_model=NewsView, operation_id="getNews")
def get_news(
    services: ServicesDep,
    principal: PrincipalDep,
    scope: ScopeQuery = "holdings",
    watchlist_id: WatchlistQuery = None,
    portfolio_id: PortfolioQuery = None,
    tickers: TickersQuery = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> NewsView:
    """The newest articles on the scope's tickers, and their daily
    sentiment over the last 30 days. Scope ``all`` is refused."""
    return services.calendars.news(
        principal, _filter(scope, watchlist_id, portfolio_id, tickers), limit=limit
    )


@router.get(
    "/earnings-warnings", response_model=EarningsWarningsView, operation_id="getEarningsWarnings"
)
def get_earnings_warnings(
    services: ServicesDep,
    principal: PrincipalDep,
    tickers: TickersQuery = None,
) -> EarningsWarningsView:
    """Which of ``tickers`` report earnings before the next open of their
    market. The order ticket shows these as a warning."""
    return services.calendars.earnings_warnings(principal, _split(tickers))


class EventAlertKindView(BaseModel):
    kind: str
    label: str
    default_days_ahead: int
    #: The switch in your notification settings that turns this kind on or
    #: off (earnings, dividends, economic).
    topic: str


@router.get(
    "/alert-kinds", response_model=list[EventAlertKindView], operation_id="listEventAlertKinds"
)
def list_event_alert_kinds() -> list[EventAlertKindView]:
    """The upcoming-event alert kinds (earnings, ex-dividend), how many
    days ahead each looks by default, and the notification switch it follows."""
    return [
        EventAlertKindView(
            kind=k.kind, label=k.label, default_days_ahead=k.default_days_ahead, topic=k.topic
        )
        for k in alert_kinds()
    ]


@router.post(
    "/refresh",
    **JOB_CREATED,
    operation_id="refreshCalendars",
    dependencies=needs(Permission.OPERATIONS_RUN),
)
def refresh_calendars(
    body: CalendarRefreshRequest, services: ServicesDep, principal: PrincipalDep, response: Response
) -> Job:
    """Queue a calendar refresh from the data source, then the
    upcoming-event notifications. Poll ``/api/jobs/{id}``."""
    return accepted(services.calendars.submit_refresh(body, owner_id=principal.user_id), response)


@router.get(
    "/refresh/{job_id}/result",
    response_model=CalendarRefreshView,
    operation_id="getCalendarRefreshResult",
)
def get_refresh_result(
    job_id: str, services: ServicesDep, principal: OptionalPrincipalDep
) -> CalendarRefreshView:
    """The result of a succeeded refresh job (409 until it has succeeded)."""
    return services.jobs.typed_result(job_id, CALENDAR_REFRESH_JOB, CalendarRefreshView, principal)
