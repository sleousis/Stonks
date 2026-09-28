"""JournalService: the round-trip journal for the CLI, the API and MCP
(roadmap 23.3). The trade math lives in :mod:`stonks.journal`; this module
scopes it to the caller and converts money to the portfolio's base currency.

Reads take a portfolio the caller already resolved (``PortfolioService.resolve``
in the API, which makes another user's portfolio a 404). A trade id is the
id of the fill that opened it; a trade of another portfolio reads as
missing. Playbooks belong to one person, and a trade can only use a
playbook of the portfolio's owner.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time
from typing import TYPE_CHECKING, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from stonks.accounts import DEFAULT_OWNER_ID, Scope
from stonks.app.context import AppContext
from stonks.app.errors import NotFoundError, ValidationError
from stonks.app.pagination import Page
from stonks.app.strategy_names import strategy_title
from stonks.auth.policy import Permission, require
from stonks.auth.principal import Principal
from stonks.journal.analytics import CalendarBucket, GroupStats, group_stats, pnl_calendar
from stonks.journal.store import (
    Annotation,
    JournalStoreError,
    Playbook,
    create_playbook,
    get_playbook,
    list_playbooks,
    load_annotations,
    save_annotation,
    update_playbook,
    used_labels,
)
from stonks.journal.trips import JournalTrade, build_trades, load_ledger

if TYPE_CHECKING:
    from stonks.fx import FxRates
    from stonks.store.state import SqliteState

Who = Scope | Principal
TradeStatus = Literal["all", "open", "closed"]
PlanFilter = Literal["followed", "broke", "not_said"]
Origin = Literal["strategy", "manual"]
BreakdownBy = Literal[
    "all",
    "sleeve",
    "origin",
    "ticker",
    "side",
    "exit_trigger",
    "tag",
    "mistake",
    "playbook",
    "plan",
]
BREAKDOWN_BYS: tuple[str, ...] = BreakdownBy.__args__  # type: ignore[attr-defined]


def _scope(who: Who) -> Scope:
    return who.scope if isinstance(who, Principal) else who


def _owner(scope: Scope) -> str:
    """Whose playbooks: the person, or the install owner for the operator."""
    return DEFAULT_OWNER_ID if scope.is_service else scope.user_id


# ---- requests and views ----------------------------------------------------------


class AnnotationRequest(BaseModel):
    """A trade's review. Replaces what was there."""

    model_config = ConfigDict(extra="forbid")

    tags: list[str] = Field(default_factory=list, max_length=20)
    mistakes: list[str] = Field(default_factory=list, max_length=20)
    playbook_id: str | None = Field(default=None, max_length=64)
    #: Did you follow your plan? ``null`` when you have not said.
    followed_plan: bool | None = None
    review: str | None = Field(default=None, max_length=4000)


class PlaybookCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=60)
    description: str | None = Field(default=None, max_length=2000)


class PlaybookUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=60)
    description: str | None = Field(default=None, max_length=2000)
    archived: bool | None = None


class PlaybookView(BaseModel):
    id: str
    name: str
    description: str | None
    archived: bool
    created_at: str
    updated_at: str

    @classmethod
    def of(cls, p: Playbook) -> PlaybookView:
        return cls(
            id=p.id,
            name=p.name,
            description=p.description,
            archived=p.archived,
            created_at=p.created_at,
            updated_at=p.updated_at,
        )


class _SleeveNamed(BaseModel):
    """A view with a ``sleeve`` (a strategy id, ``manual`` or
    ``unattributed``): adds ``sleeve_name``, the strategy's plain title."""

    sleeve_name: str | None = Field(
        default=None, description="The sleeve strategy's plain title (a starter's), or null."
    )

    @model_validator(mode="after")
    def _derive_sleeve_name(self) -> _SleeveNamed:
        self.sleeve_name = strategy_title(getattr(self, "sleeve", None))
        return self


class JournalTradeView(_SleeveNamed):
    """One leg of a trade. Money is in the instrument's currency, and
    ``pnl_base`` in the portfolio's base currency (null with no FX rate)."""

    leg_id: str
    #: The id of the fill that opened the trade; shared by all its legs.
    trade_id: int
    ticker: str
    side: Literal["long", "short"]
    #: The strategy that opened the trade, or ``manual``.
    sleeve: str
    origin: Origin
    entry_client_id: str
    exit_client_id: str | None
    entry_at: datetime
    exit_at: datetime | None
    quantity: float
    entry_price: float
    #: The exit fill's price, or the latest close while open.
    exit_price: float
    is_open: bool
    holding_days: float
    pnl: float
    return_pct: float
    fees: float
    dividends: float
    currency: str
    pnl_base: float | None
    #: Worst and best move from the entry while held, as fractions (<= 0, >= 0).
    mae_pct: float | None
    mfe_pct: float | None
    #: Where the exit sits in the range the trade saw: 0 worst, 1 best.
    exit_efficiency: float | None
    #: stop, signal, exit_no_pick, risk_rule or manual.
    exit_trigger: str | None
    stop_price: float | None
    #: order_plan or protective_stop.
    stop_source: str | None
    target_price: float | None
    #: Money at risk at the initial stop.
    risk_amount: float | None
    r_multiple: float | None
    mae_r: float | None
    tags: list[str]
    mistakes: list[str]
    playbook_id: str | None
    playbook_name: str | None
    followed_plan: bool | None
    review: str | None


class JournalTradeDetailView(_SleeveNamed):
    """A whole trade: every leg and its review."""

    trade_id: int
    portfolio_id: str
    ticker: str
    side: Literal["long", "short"]
    sleeve: str
    origin: Origin
    legs: list[JournalTradeView]
    pnl: float
    pnl_base: float | None
    tags: list[str]
    mistakes: list[str]
    playbook_id: str | None
    playbook_name: str | None
    followed_plan: bool | None
    review: str | None
    updated_by: str | None
    updated_at: str | None


class AnnotationView(BaseModel):
    trade_id: int
    tags: list[str]
    mistakes: list[str]
    playbook_id: str | None
    followed_plan: bool | None
    review: str | None
    updated_by: str
    updated_at: str


class GroupStatsView(BaseModel):
    """Results of one group of closed legs, money in the base currency."""

    key: str
    trades: int
    wins: int
    losses: int
    open: int
    win_rate: float | None
    pnl: float
    avg_pnl: float | None
    avg_win: float | None
    avg_loss: float | None
    profit_factor: float | None
    avg_r: float | None
    r_trades: int
    avg_holding_days: float | None
    avg_exit_efficiency: float | None

    @classmethod
    def of(cls, g: GroupStats) -> GroupStatsView:
        return cls(**g.__dict__)


class BreakdownView(BaseModel):
    portfolio_id: str
    by: BreakdownBy
    base_currency: str
    since: date | None
    until: date | None
    groups: list[GroupStatsView]
    #: Legs left out because their currency has no FX rate.
    unconverted: int
    fx_missing: list[str]


class CalendarBucketView(BaseModel):
    key: str
    start: date
    end: date
    pnl: float
    trades: int
    wins: int

    @classmethod
    def of(cls, b: CalendarBucket) -> CalendarBucketView:
        return cls(**b.__dict__)


class PnlCalendarView(BaseModel):
    """Realised P&L of closed legs by exit day, ISO week and month."""

    portfolio_id: str
    base_currency: str
    since: date | None
    until: date | None
    days: list[CalendarBucketView]
    weeks: list[CalendarBucketView]
    months: list[CalendarBucketView]
    total: float
    trades: int
    best_day: str | None
    worst_day: str | None
    unconverted: int
    fx_missing: list[str]


class JournalLabelsView(BaseModel):
    """Tags and mistakes already used in the portfolio, for suggestions."""

    tags: list[str]
    mistakes: list[str]


@dataclass(frozen=True)
class TradeFilter:
    status: TradeStatus = "all"
    sleeve: str | None = None
    origin: Origin | None = None
    ticker: str | None = None
    #: Closed legs by exit day, open legs by entry day.
    since: date | None = None
    until: date | None = None
    tag: str | None = None
    mistake: str | None = None
    playbook_id: str | None = None
    plan: PlanFilter | None = None


# ---- the book as the service sees it ---------------------------------------------


@dataclass(frozen=True)
class _Book:
    portfolio_id: str
    owner_id: str
    base_currency: str
    trades: list[JournalTrade]
    #: leg id -> (currency, P&L in the base currency or None).
    money: dict[str, tuple[str, float | None]]
    annotations: dict[int, Annotation]
    playbooks: dict[str, str]
    fx_missing: list[str]


def _plan_key(annotation: Annotation | None) -> str:
    if annotation is None or annotation.followed_plan is None:
        return "not_said"
    return "followed" if annotation.followed_plan else "broke"


def _day_of(trade: JournalTrade) -> date:
    return (trade.exit_at or trade.entry_at).date()


class JournalService:
    def __init__(self, context: AppContext, *, now: datetime | None = None) -> None:
        self._context = context
        self._now = now

    # ---- reads -------------------------------------------------------------------

    def trades(
        self,
        portfolio_id: str,
        filters: TradeFilter | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Page[JournalTradeView]:
        """One portfolio's legs, open first then newest exit first."""
        book = self._book(portfolio_id)
        chosen = self._filter(book, filters or TradeFilter())
        chosen = sorted((t for t in chosen if t.is_open), key=_sort_ts, reverse=True) + sorted(
            (t for t in chosen if not t.is_open), key=_sort_ts, reverse=True
        )
        window = chosen[offset : offset + limit]
        return Page[JournalTradeView](
            items=[self._view(book, t) for t in window],
            total=len(chosen),
            limit=limit,
            offset=offset,
        )

    def trade(self, portfolio_id: str, trade_id: int) -> JournalTradeDetailView:
        """One trade with every leg and its review."""
        book = self._book(portfolio_id)
        legs = [t for t in book.trades if t.trade_id == trade_id]
        if not legs:
            raise NotFoundError(f"trade {trade_id} not found")
        views = [self._view(book, t) for t in legs]
        ann = book.annotations.get(trade_id)
        bases = [v.pnl_base for v in views]
        first = legs[0]
        return JournalTradeDetailView(
            trade_id=trade_id,
            portfolio_id=portfolio_id,
            ticker=first.ticker,
            side=first.side,
            sleeve=first.sleeve,
            origin=first.origin,
            legs=views,
            pnl=sum(t.pnl for t in legs),
            pnl_base=None if any(b is None for b in bases) else sum(b or 0.0 for b in bases),
            tags=list(ann.tags) if ann else [],
            mistakes=list(ann.mistakes) if ann else [],
            playbook_id=ann.playbook_id if ann else None,
            playbook_name=book.playbooks.get(ann.playbook_id or "") if ann else None,
            followed_plan=ann.followed_plan if ann else None,
            review=ann.review if ann else None,
            updated_by=ann.updated_by if ann else None,
            updated_at=ann.updated_at if ann else None,
        )

    def calendar(
        self,
        portfolio_id: str,
        *,
        since: date | None = None,
        until: date | None = None,
        sleeve: str | None = None,
        origin: Origin | None = None,
    ) -> PnlCalendarView:
        """Realised P&L by exit day, ISO week and month, in the base currency."""
        book = self._book(portfolio_id)
        chosen = self._filter(
            book,
            TradeFilter(status="closed", since=since, until=until, sleeve=sleeve, origin=origin),
        )
        cal = pnl_calendar(chosen, lambda t: book.money[t.leg_id][1])
        return PnlCalendarView(
            portfolio_id=portfolio_id,
            base_currency=book.base_currency,
            since=since,
            until=until,
            days=[CalendarBucketView.of(b) for b in cal.days],
            weeks=[CalendarBucketView.of(b) for b in cal.weeks],
            months=[CalendarBucketView.of(b) for b in cal.months],
            total=cal.total,
            trades=cal.trades,
            best_day=cal.best_day,
            worst_day=cal.worst_day,
            unconverted=cal.unconverted,
            fx_missing=book.fx_missing,
        )

    def breakdown(
        self,
        portfolio_id: str,
        *,
        by: BreakdownBy = "all",
        since: date | None = None,
        until: date | None = None,
        sleeve: str | None = None,
        origin: Origin | None = None,
    ) -> BreakdownView:
        """Results grouped ``by`` sleeve, origin, ticker, side, exit trigger,
        tag, mistake, playbook or plan (followed, broke, not said)."""
        if by not in BREAKDOWN_BYS:
            raise ValidationError(f"by must be one of {', '.join(BREAKDOWN_BYS)}")
        book = self._book(portfolio_id)
        chosen = self._filter(
            book, TradeFilter(since=since, until=until, sleeve=sleeve, origin=origin)
        )
        converted: list[JournalTrade] = []
        unconverted = 0
        for t in chosen:
            base = book.money[t.leg_id][1]
            if base is None:
                unconverted += 1
                continue
            converted.append(_in_base(t, base))
        groups = group_stats(converted, lambda t: self._keys(book, t, by))
        return BreakdownView(
            portfolio_id=portfolio_id,
            by=by,
            base_currency=book.base_currency,
            since=since,
            until=until,
            groups=[GroupStatsView.of(g) for g in groups],
            unconverted=unconverted,
            fx_missing=book.fx_missing,
        )

    def labels(self, portfolio_id: str) -> JournalLabelsView:
        with self._context.state() as state:
            used = used_labels(state, portfolio_id)
        return JournalLabelsView(tags=used["tag"], mistakes=used["mistake"])

    def playbooks(self, who: Who, *, include_archived: bool = False) -> list[PlaybookView]:
        """The caller's playbooks, by name."""
        owner = _owner(_scope(who))
        with self._context.state() as state:
            found = list_playbooks(state, owner, include_archived=include_archived)
        return [PlaybookView.of(p) for p in found]

    # ---- writes ------------------------------------------------------------------

    def annotate(
        self, who: Who, portfolio_id: str, trade_id: int, request: AnnotationRequest
    ) -> AnnotationView:
        """Replace one trade's tags, mistakes, playbook, plan flag and review."""
        scope = _scope(who)
        if isinstance(who, Principal):
            require(who, Permission.PORTFOLIO_MANAGE)
        with self._context.state() as state:
            ledger = load_ledger(state, portfolio_id)
            entries = {t.trade_id for t in build_trades(ledger, bars=None, now=self._clock())}
            if trade_id not in entries:
                raise NotFoundError(f"trade {trade_id} not found")
            if request.playbook_id is not None:
                owner = self._portfolio_owner(state, portfolio_id)
                playbook = get_playbook(state, request.playbook_id)
                if playbook is None or playbook.owner_id != owner:
                    raise NotFoundError(f"playbook {request.playbook_id!r} not found")
            try:
                saved = save_annotation(
                    state,
                    portfolio_id,
                    trade_id,
                    playbook_id=request.playbook_id,
                    followed_plan=request.followed_plan,
                    review=request.review,
                    tags=request.tags,
                    mistakes=request.mistakes,
                    actor=scope.actor,
                )
            except JournalStoreError as exc:
                raise ValidationError(str(exc)) from None
        return AnnotationView(
            trade_id=saved.trade_id,
            tags=list(saved.tags),
            mistakes=list(saved.mistakes),
            playbook_id=saved.playbook_id,
            followed_plan=saved.followed_plan,
            review=saved.review,
            updated_by=saved.updated_by,
            updated_at=saved.updated_at,
        )

    def create_playbook(self, who: Who, request: PlaybookCreate) -> PlaybookView:
        scope = _scope(who)
        if isinstance(who, Principal):
            require(who, Permission.PORTFOLIO_MANAGE)
        with self._context.state() as state:
            try:
                made = create_playbook(
                    state, _owner(scope), name=request.name, description=request.description
                )
            except JournalStoreError as exc:
                raise ValidationError(str(exc)) from None
        return PlaybookView.of(made)

    def update_playbook(self, who: Who, playbook_id: str, request: PlaybookUpdate) -> PlaybookView:
        scope = _scope(who)
        if isinstance(who, Principal):
            require(who, Permission.PORTFOLIO_MANAGE)
        with self._context.state() as state:
            playbook = get_playbook(state, playbook_id)
            if playbook is None or playbook.owner_id != _owner(scope):
                raise NotFoundError(f"playbook {playbook_id!r} not found")
            try:
                updated = update_playbook(
                    state,
                    playbook,
                    name=request.name,
                    description=request.description,
                    archived=request.archived,
                )
            except JournalStoreError as exc:
                raise ValidationError(str(exc)) from None
        return PlaybookView.of(updated)

    # ---- helpers -----------------------------------------------------------------

    def _clock(self) -> datetime:
        return self._now or datetime.now(UTC)

    @staticmethod
    def _portfolio_owner(state: SqliteState, portfolio_id: str) -> str:
        rows = state.sql("SELECT owner_id FROM portfolios WHERE id = ?", [portfolio_id])
        if not rows:
            raise NotFoundError(f"portfolio {portfolio_id!r} not found")
        return str(rows[0]["owner_id"])

    def _book(self, portfolio_id: str) -> _Book:
        now = self._clock()
        with self._context.state() as state:
            ledger = load_ledger(state, portfolio_id)
            annotations = load_annotations(state, portfolio_id)
            found = state.sql(
                "SELECT owner_id, base_currency FROM portfolios WHERE id = ?", [portfolio_id]
            )
            if not found:
                raise NotFoundError(f"portfolio {portfolio_id!r} not found")
            owner = str(found[0]["owner_id"])
            base = str(found[0]["base_currency"] or "USD").upper()
            playbooks = {p.id: p.name for p in list_playbooks(state, owner, include_archived=True)}
        tickers = sorted({f.ticker for f in ledger.fills})
        start = min((f.filled_at for f in ledger.fills), default=now)
        bars, currencies, fx = self._market(tickers, start, now, base)
        trades = build_trades(ledger, bars=bars, now=now)
        money: dict[str, tuple[str, float | None]] = {}
        missing: set[str] = set()
        for t in trades:
            ccy = currencies.get(t.ticker, base)
            rate = 1.0 if ccy == base else (fx.rate(ccy, base, _day_of(t)) if fx else None)
            if rate is None:
                missing.add(ccy)
            money[t.leg_id] = (ccy, t.pnl * rate if rate is not None else None)
        return _Book(
            portfolio_id=portfolio_id,
            owner_id=owner,
            base_currency=base,
            trades=trades,
            money=money,
            annotations=annotations,
            playbooks=playbooks,
            fx_missing=sorted(missing),
        )

    def _market(
        self, tickers: Sequence[str], start: datetime, now: datetime, base: str
    ) -> tuple[pd.DataFrame | None, dict[str, str], FxRates | None]:
        """Daily bars from the first entry day, instrument currencies and FX."""
        from stonks.core.interval import Interval
        from stonks.fx import load_fx_rates

        if not tickers:
            return None, {}, None
        lo = datetime.combine(start.date(), time.min)
        hi = datetime.combine(now.date(), time.max)
        frames: list[pd.DataFrame] = []
        fx: FxRates | None = None
        with self._context.lake() as lake:
            for ticker in tickers:
                df = lake.get_bars(ticker, Interval.DAY_1, lo, hi)
                if not df.empty:
                    frames.append(df.assign(ticker=ticker))
            cur = lake.sql(
                "SELECT id, currency FROM instruments WHERE id = ANY(?) AND currency IS NOT NULL",
                [list(tickers)],
            )
            currencies = {
                str(i): str(c).upper() for i, c in zip(cur["id"], cur["currency"], strict=True)
            }
            if any(c != base for c in currencies.values()):
                fx = load_fx_rates(lake, {*currencies.values(), base})
        bars = pd.concat(frames, ignore_index=True) if frames else None
        return bars, currencies, fx

    def _filter(self, book: _Book, f: TradeFilter) -> list[JournalTrade]:
        out: list[JournalTrade] = []
        for t in book.trades:
            ann = book.annotations.get(t.trade_id)
            if f.status == "open" and not t.is_open:
                continue
            if f.status == "closed" and t.is_open:
                continue
            if f.sleeve is not None and t.sleeve != f.sleeve:
                continue
            if f.origin is not None and t.origin != f.origin:
                continue
            if f.ticker is not None and t.ticker != f.ticker:
                continue
            day = _day_of(t)
            if f.since is not None and day < f.since:
                continue
            if f.until is not None and day > f.until:
                continue
            if f.tag is not None and (ann is None or f.tag.strip().lower() not in ann.tags):
                continue
            if f.mistake is not None and (
                ann is None or f.mistake.strip().lower() not in ann.mistakes
            ):
                continue
            if f.playbook_id is not None and (ann is None or ann.playbook_id != f.playbook_id):
                continue
            if f.plan is not None and _plan_key(ann) != f.plan:
                continue
            out.append(t)
        return out

    @staticmethod
    def _keys(book: _Book, t: JournalTrade, by: str) -> Iterable[str]:
        ann = book.annotations.get(t.trade_id)
        match by:
            case "all":
                return ["all"]
            case "sleeve":
                return [t.sleeve]
            case "origin":
                return [t.origin]
            case "ticker":
                return [t.ticker]
            case "side":
                return [t.side]
            case "exit_trigger":
                return [t.exit_trigger or ("open" if t.is_open else "unknown")]
            case "tag":
                return list(ann.tags) if ann and ann.tags else ["untagged"]
            case "mistake":
                return list(ann.mistakes) if ann and ann.mistakes else ["none"]
            case "playbook":
                name = book.playbooks.get(ann.playbook_id) if ann and ann.playbook_id else None
                return [name or "none"]
            case _:
                return [_plan_key(ann)]

    @staticmethod
    def _view(book: _Book, t: JournalTrade) -> JournalTradeView:
        ann = book.annotations.get(t.trade_id)
        currency, base = book.money[t.leg_id]
        return JournalTradeView(
            leg_id=t.leg_id,
            trade_id=t.trade_id,
            ticker=t.ticker,
            side=t.side,
            sleeve=t.sleeve,
            origin=t.origin,
            entry_client_id=t.entry_client_id,
            exit_client_id=t.exit_client_id,
            entry_at=t.entry_at,
            exit_at=t.exit_at,
            quantity=t.quantity,
            entry_price=t.entry_price,
            exit_price=t.exit_price,
            is_open=t.is_open,
            holding_days=t.holding_days,
            pnl=t.pnl,
            return_pct=t.return_pct,
            fees=t.fees,
            dividends=t.dividends,
            currency=currency,
            pnl_base=base,
            mae_pct=t.mae_pct,
            mfe_pct=t.mfe_pct,
            exit_efficiency=t.exit_efficiency,
            exit_trigger=t.exit_trigger,
            stop_price=t.stop_price,
            stop_source=t.stop_source,
            target_price=t.target_price,
            risk_amount=t.risk_amount,
            r_multiple=t.r_multiple,
            mae_r=t.mae_r,
            tags=list(ann.tags) if ann else [],
            mistakes=list(ann.mistakes) if ann else [],
            playbook_id=ann.playbook_id if ann else None,
            playbook_name=book.playbooks.get(ann.playbook_id) if ann and ann.playbook_id else None,
            followed_plan=ann.followed_plan if ann else None,
            review=ann.review if ann else None,
        )


def _sort_ts(t: JournalTrade) -> datetime:
    return t.exit_at or t.entry_at


def _in_base(t: JournalTrade, pnl_base: float) -> JournalTrade:
    """``t`` with its P&L in the base currency, for group results."""
    return replace(t, pnl=pnl_base)
