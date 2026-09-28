"""CalendarService: event calendars and news for a person (roadmap 20.7).

Reads are scoped to what the caller follows:

- ``all``: every event in the lake (capped at ``MAX_EVENTS`` per calendar);
- ``holdings``: what the caller's portfolios hold (``portfolio_id`` narrows
  it to one of them, checked like every portfolio read);
- ``watchlists``: the caller's watchlists (``watchlist_id`` picks one);
- ``tickers``: the tickers given.

Economic events are market wide: every scope shows them, filtered by
``countries`` when given.

The refresh job (``calendars_refresh``, on the lake write lane) pulls the
calendars through :meth:`IngestPipeline.run_calendars` and then sends the
upcoming-event notifications (:func:`stonks.calendars.alerts.check_event_alerts`).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, model_validator

from stonks.app.context import AppContext
from stonks.app.errors import NotFoundError, ValidationError
from stonks.app.jobs import Job, JobContext, JobRunner
from stonks.app.universes import LAKE_WRITE_LANE
from stonks.auth.policy import Permission, require
from stonks.auth.principal import Principal
from stonks.calendars.alerts import check_event_alerts
from stonks.calendars.models import (
    DividendEvent,
    EarningsEvent,
    EconomicEvent,
    FilingEvent,
    NewsItem,
    SentimentDay,
)
from stonks.calendars.store import CalendarStore
from stonks.calendars.timing import EarningsWarning, earnings_before_next_open
from stonks.calendars.tracking import held_tickers, owned_portfolio_ids, watchlist_tickers
from stonks.ingest.pipeline import CALENDAR_KINDS, CalendarKind
from stonks.ingest.wiring import build_ingest_pipeline
from stonks.notify.router import configured_router
from stonks.scheduling.calendar import calendar_for_ticker

CALENDAR_REFRESH_JOB = "calendars_refresh"

CalendarScope = Literal["all", "holdings", "watchlists", "tickers"]

#: Most events of one calendar a read returns.
MAX_EVENTS = 2000
#: Longest window one calendar read may span.
MAX_WINDOW_DAYS = 120
#: Most tickers a request may name.
MAX_TICKERS = 500


class CalendarFilter(BaseModel):
    scope: CalendarScope = "holdings"
    watchlist_id: str | None = Field(default=None, max_length=64)
    portfolio_id: str | None = Field(default=None, max_length=64)
    tickers: list[str] = Field(default_factory=list, max_length=MAX_TICKERS)

    @model_validator(mode="after")
    def _check(self) -> Self:
        self.tickers = list(dict.fromkeys(t.strip().upper() for t in self.tickers if t.strip()))
        if self.scope == "tickers" and not self.tickers:
            raise ValueError("scope 'tickers' needs at least one ticker")
        return self


class CalendarView(BaseModel):
    start: date
    end: date
    scope: CalendarScope
    #: The tickers the scope resolved to (``None`` for ``all``).
    tickers: list[str] | None
    earnings: list[EarningsEvent]
    dividends: list[DividendEvent]
    economic: list[EconomicEvent]
    #: Current reports (8-K) the scope's companies filed in the window, from
    #: ``stonks ingest edgar`` (roadmap 23.13). Empty for scope ``all``.
    filings: list[FilingEvent] = []
    #: A calendar hit ``MAX_EVENTS``; narrow the scope or the window.
    truncated: bool = False


class NewsView(BaseModel):
    tickers: list[str]
    items: list[NewsItem]
    #: Daily sentiment over the last 30 days, oldest first.
    sentiment: list[SentimentDay]


class EarningsWarningsView(BaseModel):
    checked: list[str]
    warnings: list[EarningsWarning]


class CalendarRefreshRequest(BaseModel):
    source: Literal["eodhd", "yahoo", "defillama"] = "eodhd"
    #: First day to fetch (default: a week ago).
    start: date | None = None
    #: Last day to fetch (default: five weeks ahead).
    end: date | None = None
    #: Earnings and dividends for these tickers only (default: the whole market).
    tickers: list[str] | None = Field(default=None, max_length=5000)
    #: Economic events of these countries only, ISO alpha-2 (default: all).
    countries: list[str] | None = Field(default=None, max_length=100)
    kinds: list[CalendarKind] = Field(default_factory=lambda: list(CALENDAR_KINDS), min_length=1)
    #: Send the upcoming-event notifications after the refresh.
    alerts: bool = True
    #: Look-ahead days per alert kind (``0`` turns one off).
    alert_days: dict[str, int] | None = None

    @model_validator(mode="after")
    def _window(self) -> Self:
        if self.start and self.end and self.start > self.end:
            raise ValueError("start must be on or before end")
        return self


class EventAlertSummary(BaseModel):
    people: int
    sent: int
    repeats: int


class CalendarRefreshView(BaseModel):
    run_id: int
    status: str
    start: date
    end: date
    calendars_ok: int
    calendars_failed: int
    failed: list[str]
    alerts: EventAlertSummary | None = None


def _utcnow() -> datetime:
    return datetime.now(UTC)


class CalendarService:
    def __init__(
        self,
        context: AppContext,
        runner: JobRunner | None = None,
        *,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._ctx = context
        self._clock = clock
        if runner is not None:
            runner.register(CALENDAR_REFRESH_JOB, self._handle_refresh, lock=LAKE_WRITE_LANE)
        self._runner = runner

    # ---- scope -------------------------------------------------------------------

    def resolve(self, principal: Principal, flt: CalendarFilter) -> list[str] | None:
        """The tickers ``flt`` covers for ``principal`` (``None``: all)."""
        require(principal, Permission.READ)
        if flt.scope == "all":
            return None
        if flt.scope == "tickers":
            return flt.tickers
        with self._ctx.state() as state:
            if flt.scope == "watchlists":
                if flt.watchlist_id is not None:
                    rows = state.sql(
                        "SELECT 1 FROM watchlists WHERE id = ? AND owner_id = ?",
                        [flt.watchlist_id, principal.user_id],
                    )
                    if not rows:
                        raise NotFoundError(f"no watchlist with id {flt.watchlist_id!r}")
                return watchlist_tickers(state, principal.user_id, flt.watchlist_id)
            if flt.portfolio_id is not None:
                from stonks.accounts.models import NotFound
                from stonks.accounts.scope import owned_portfolio

                try:
                    ids = [owned_portfolio(state, principal.scope, flt.portfolio_id).id]
                except NotFound as exc:
                    raise NotFoundError(str(exc)) from None
            else:
                ids = owned_portfolio_ids(state, principal.user_id)
            return held_tickers(state, ids)

    # ---- reads -------------------------------------------------------------------

    def calendar(
        self,
        principal: Principal,
        flt: CalendarFilter,
        *,
        start: date | None = None,
        end: date | None = None,
        countries: Sequence[str] | None = None,
    ) -> CalendarView:
        """Earnings, ex-dividend dates and economic releases from ``start``
        (default today) to ``end`` (default two weeks later)."""
        tickers = self.resolve(principal, flt)
        today = self._clock().date()
        start = start or today
        end = end or start + timedelta(days=13)
        if end < start:
            raise ValidationError("end must be on or after start")
        if (end - start).days > MAX_WINDOW_DAYS:
            raise ValidationError(f"a calendar spans at most {MAX_WINDOW_DAYS} days")
        with self._ctx.lake() as lake:
            store = CalendarStore(lake)
            earnings = store.earnings(start, end, tickers=tickers)
            dividends = store.dividends(start, end, tickers=tickers)
            economic = store.economic(start, end, countries=countries or None)
            filings = store.filings(start, end, tickers=tickers) if tickers is not None else []
        truncated = any(len(x) > MAX_EVENTS for x in (earnings, dividends, economic, filings))
        return CalendarView(
            start=start,
            end=end,
            scope=flt.scope,
            tickers=tickers,
            earnings=earnings[:MAX_EVENTS],
            dividends=dividends[:MAX_EVENTS],
            economic=economic[:MAX_EVENTS],
            filings=filings[:MAX_EVENTS],
            truncated=truncated,
        )

    def news(self, principal: Principal, flt: CalendarFilter, *, limit: int = 50) -> NewsView:
        """The newest articles on the scope's tickers and their daily
        sentiment. ``all`` is not a news scope: name tickers or a list."""
        if flt.scope == "all":
            raise ValidationError("news needs tickers, a watchlist or your holdings")
        tickers = self.resolve(principal, flt) or []
        since = self._clock().date() - timedelta(days=30)
        with self._ctx.lake() as lake:
            store = CalendarStore(lake)
            items = store.news(tickers, limit=limit)
            sentiment = store.sentiment(tickers, since=since)
        return NewsView(tickers=tickers, items=items, sentiment=sentiment)

    def earnings_warnings(
        self, principal: Principal, tickers: Sequence[str]
    ) -> EarningsWarningsView:
        """Which of ``tickers`` report earnings between now and the next
        open of their market (the order ticket warning)."""
        require(principal, Permission.READ)
        picked = list(dict.fromkeys(t.strip().upper() for t in tickers if t.strip()))
        if len(picked) > MAX_TICKERS:
            raise ValidationError(f"check at most {MAX_TICKERS} tickers at once")
        now = self._clock()
        with self._ctx.lake() as lake:
            events = CalendarStore(lake).earnings(
                now.date() - timedelta(days=1), now.date() + timedelta(days=10), tickers=picked
            )
            classes = lake.get_asset_classes(picked) if picked else {}
        warnings = earnings_before_next_open(
            events, now=now, calendar_for=lambda t: calendar_for_ticker(t, classes.get(t))
        )
        return EarningsWarningsView(checked=picked, warnings=warnings)

    # ---- refresh -----------------------------------------------------------------

    def submit_refresh(
        self, request: CalendarRefreshRequest, *, owner_id: str | None = None
    ) -> Job:
        if self._runner is None:  # pragma: no cover - wiring error
            raise RuntimeError("CalendarService has no job runner")
        self._ctx.build_source(request.source)  # fail fast when it isn't configured
        return self._runner.submit(
            CALENDAR_REFRESH_JOB, request.model_dump(mode="json"), owner_id=owner_id
        )

    def refresh(
        self, request: CalendarRefreshRequest, progress: JobContext | None = None
    ) -> CalendarRefreshView:
        today = self._clock().date()
        start = request.start or today - timedelta(days=7)
        end = request.end or today + timedelta(days=35)
        source = self._ctx.build_source(request.source)
        with self._ctx.lake() as lake:
            pipeline = build_ingest_pipeline(self._ctx.settings, source, lake)
            result = pipeline.run_calendars(
                start,
                end,
                tickers=request.tickers,
                countries=request.countries,
                kinds=request.kinds,
            )
            if progress is not None:
                progress.progress(0.8, f"calendars {result.status}")
            summary = None
            if request.alerts:
                with self._ctx.state() as state:
                    report = check_event_alerts(
                        lake, configured_router(state), today, days_ahead=request.alert_days
                    )
                summary = EventAlertSummary(
                    people=report.people, sent=report.sent, repeats=report.repeats
                )
        return CalendarRefreshView(
            run_id=result.run_id,
            status=result.status,
            start=start,
            end=end,
            calendars_ok=result.tickers_ok,
            calendars_failed=result.tickers_failed,
            failed=list(result.failed),
            alerts=summary,
        )

    def _handle_refresh(self, params: dict[str, Any], ctx: JobContext) -> CalendarRefreshView:
        return self.refresh(CalendarRefreshRequest.model_validate(params), progress=ctx)
