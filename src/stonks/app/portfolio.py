"""PortfolioService — the book as of the latest tick, valued at the latest
stored closes, plus snapshot history.

Every read is of one portfolio the caller owns: :meth:`PortfolioService.resolve`
turns a principal and an optional ``portfolio_id`` into that id (404 for a
portfolio that isn't yours, admins included). Admins get
:meth:`PortfolioService.totals`, sums across every book, never holdings."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from stonks.accounts import AccountsError, NotFound, PortfolioRepository, owned_portfolio
from stonks.accounts import Portfolio as AccountPortfolio
from stonks.accounts.models import DEFAULT_PORTFOLIO_ID
from stonks.app.context import AppContext
from stonks.app.cost_basis import FillLot, average_costs
from stonks.app.errors import NotFoundError, ValidationError
from stonks.app.pagination import Page
from stonks.auth.policy import Permission, require
from stonks.auth.principal import Principal
from stonks.production.ledger import ledger_filter

#: Reporting currency when the held instruments don't agree on one (or the
#: book is empty, or the lake has no currency for them).
DEFAULT_CURRENCY = "USD"


class PositionView(BaseModel):
    ticker: str
    quantity: float
    price: float | None
    price_date: date | None
    market_value: float | None
    weight: float | None
    currency: str | None = Field(
        default=None, description="The instrument's trading currency; null when unknown."
    )
    avg_cost: float | None = Field(
        default=None,
        description="Average cost per share from the fill history (weighted average, fees "
        "included); null when the ledger has no fills for the position.",
    )
    cost_basis: float | None = Field(default=None, description="avg_cost * quantity.")
    unrealized_pnl: float | None = Field(
        default=None, description="(price - avg_cost) * quantity at the latest stored close."
    )
    unrealized_pnl_pct: float | None = Field(
        default=None, description="unrealized_pnl / |cost_basis| (0.05 = +5%)."
    )


class PortfolioView(BaseModel):
    #: When the underlying snapshot was taken; ``None`` before the first tick
    #: (the book is then the configured initial cash).
    taken_at: datetime | None
    tick_id: str | None
    cash: float
    positions: list[PositionView]
    positions_value: float
    total_value: float
    #: Total value recorded at snapshot time (valued at that tick's prices).
    snapshot_total_value: float | None
    currency: str = Field(
        default=DEFAULT_CURRENCY,
        description="Reporting currency: the currency every held instrument shares, else "
        f"{DEFAULT_CURRENCY!r} (also for an empty book or unknown currencies). Amounts are not "
        "FX-converted.",
    )
    cost_basis: float = Field(
        default=0.0, description="Sum of the positions' cost_basis (positions with a known cost)."
    )
    unrealized_pnl: float = Field(
        default=0.0, description="Sum of the positions' unrealized_pnl (priced, known cost)."
    )


class SnapshotView(BaseModel):
    id: int
    tick_id: str | None
    taken_at: datetime
    cash: float
    positions: dict[str, float]
    total_value: float


class PortfolioTotalsView(BaseModel):
    """Sums over every active portfolio's latest snapshot, for admins. No
    tickers and no per-person numbers (decision 2026-09-26)."""

    portfolios: int = Field(description="Active portfolios with at least one snapshot.")
    owners: int = Field(description="People who own those portfolios.")
    cash: float
    total_value: float = Field(description="Sum of each book's value at its latest snapshot.")
    suppressed: bool = Field(
        default=False,
        description="True when too few other people own books for the sums to hide anyone's "
        "numbers: cash and value then read 0.",
    )


#: Fewest owners besides the viewing admin before money totals are shown,
#: so no sum reveals one person's numbers (BE-43).
TOTALS_MIN_OWNERS = 3

#: Active books with at least one snapshot. A broker book's paper account
#: (``paper_of``) is the same person's book, so it is left out.
TOTALS_PORTFOLIOS_SQL = (
    "SELECT p.id, p.owner_id FROM portfolios p WHERE p.status = 'active' AND p.paper_of IS NULL"
    " AND EXISTS (SELECT 1 FROM portfolio_snapshots s WHERE s.portfolio_id = p.id)"
    " ORDER BY p.id"
)


def totals_suppressed(owner_ids: set[str], viewer_id: str) -> bool:
    """Whether money totals over books owned by ``owner_ids`` would expose
    someone other than the viewer: fewer than :data:`TOTALS_MIN_OWNERS`
    other owners (books that are all the viewer's own are always shown)."""
    others = owner_ids - {viewer_id}
    return 0 < len(others) < TOTALS_MIN_OWNERS


Trading = Literal["paper", "live"]
BrokerKind = Literal["simulated", "alpaca", "connection"]

PortfolioName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)
]


class PortfolioCreate(BaseModel):
    """A new paper portfolio of yours (simulated fills on the Stonks ledger).
    Broker portfolios come from linking a broker connection."""

    model_config = ConfigDict(extra="forbid")

    name: PortfolioName
    initial_cash: float | None = Field(
        default=None, gt=0, le=1e12, description="Starting cash. Default: the configured amount."
    )
    base_currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")


class PortfolioRename(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: PortfolioName


class TradingModeView(BaseModel):
    """Whether a portfolio trades paper or live money, and through what."""

    portfolio_id: str
    name: str
    trading: Trading = Field(
        description="paper: simulated fills or a paper broker account. live: real money."
    )
    broker: BrokerKind = Field(
        description="simulated (the Stonks ledger), alpaca (the configured account, default "
        "portfolio only) or connection (a linked broker account, synced read-only)."
    )
    detail: str


class PortfolioSummaryView(BaseModel):
    id: str
    name: str
    kind: Literal["simulated", "broker"]
    status: Literal["active", "paused", "archived"]
    base_currency: str
    initial_cash: float | None
    broker_connection_id: str | None
    trading: Trading
    created_at: datetime
    #: The portfolio reads use when no ``portfolio_id`` is sent.
    is_default: bool = False


class PortfolioService:
    def __init__(self, context: AppContext) -> None:
        self._ctx = context

    def resolve(self, principal: Principal, portfolio_id: str | None = None) -> str:
        """The portfolio a read is about. ``portfolio_id`` must be the
        caller's (``NotFoundError`` otherwise, so ids don't leak). Without
        one: the default portfolio when the caller owns it, else their
        oldest open one. A service principal reads any book."""
        require(principal, Permission.READ)
        scope = principal.scope
        with self._ctx.state() as state:
            if portfolio_id is not None:
                try:
                    return owned_portfolio(state, scope, portfolio_id).id
                except NotFound as exc:
                    raise NotFoundError(str(exc)) from None
            if scope.is_service:
                return DEFAULT_PORTFOLIO_ID
            rows = state.sql(
                "SELECT p.id FROM portfolios p JOIN users u ON u.id = p.owner_id"
                " WHERE p.owner_id = ? AND u.status = 'active' AND p.status != 'archived'"
                " ORDER BY p.id != ?, p.created_at, p.id LIMIT 1",
                [scope.user_id, DEFAULT_PORTFOLIO_ID],
            )
        if not rows:
            raise NotFoundError("you have no portfolio yet")
        return rows[0]["id"]

    def list_mine(self, principal: Principal) -> list[PortfolioSummaryView]:
        """Your portfolios, oldest first (admins too: never other people's)."""
        require(principal, Permission.READ)
        with self._ctx.state() as state:
            books = PortfolioRepository(state).list(principal.scope)
        modes = {m.portfolio_id: m for m in self._modes(books)}
        open_books = sorted(
            (p for p in books if p.status != "archived"),
            key=lambda p: (p.id != DEFAULT_PORTFOLIO_ID, p.created_at, p.id),
        )
        default_id = open_books[0].id if open_books else None
        return [
            PortfolioSummaryView(
                id=p.id,
                name=p.name,
                kind=p.kind,
                status=p.status,
                base_currency=p.base_currency,
                initial_cash=p.initial_cash,
                broker_connection_id=p.broker_connection_id,
                trading=modes[p.id].trading,
                created_at=datetime.fromisoformat(p.created_at),
                is_default=p.id == default_id,
            )
            for p in books
        ]

    def create(self, principal: Principal, body: PortfolioCreate) -> PortfolioSummaryView:
        """Open a new paper portfolio owned by the caller."""
        require(principal, Permission.PORTFOLIO_MANAGE)
        with self._ctx.state() as state:
            try:
                made = PortfolioRepository(state).create(
                    principal.scope,
                    name=body.name,
                    base_currency=body.base_currency,
                    initial_cash=body.initial_cash,
                )
            except AccountsError as exc:
                raise ValidationError(str(exc)) from None
        return self._summary(principal, made.id)

    def rename(
        self, principal: Principal, portfolio_id: str, body: PortfolioRename
    ) -> PortfolioSummaryView:
        """Rename one of your portfolios (404 when it isn't yours)."""
        require(principal, Permission.PORTFOLIO_MANAGE)
        with self._ctx.state() as state:
            try:
                PortfolioRepository(state).rename(principal.scope, portfolio_id, body.name)
            except NotFound as exc:
                raise NotFoundError(str(exc)) from None
        return self._summary(principal, portfolio_id)

    def _summary(self, principal: Principal, portfolio_id: str) -> PortfolioSummaryView:
        for view in self.list_mine(principal):
            if view.id == portfolio_id:
                return view
        raise NotFoundError(f"portfolio {portfolio_id} not found")

    def trading_modes(self, principal: Principal) -> list[TradingModeView]:
        """For each of your portfolios: paper or live, and the broker."""
        require(principal, Permission.READ)
        with self._ctx.state() as state:
            books = PortfolioRepository(state).list(principal.scope)
        return self._modes(books)

    def _modes(self, books: list[AccountPortfolio]) -> list[TradingModeView]:
        brokers = self._ctx.settings.brokers
        out: list[TradingModeView] = []
        for p in books:
            if p.id == DEFAULT_PORTFOLIO_ID and brokers.kind == "alpaca":
                live = not brokers.alpaca.paper and brokers.alpaca.allow_live
                trading: Trading = "live" if live else "paper"
                broker: BrokerKind = "alpaca"
                detail = f"orders go to the Alpaca {'live' if live else 'paper'} account"
            elif p.kind == "broker":
                trading, broker = "live", "connection"
                detail = "mirrors a real broker account through its connection (read-only sync)"
            else:
                trading, broker = "paper", "simulated"
                detail = "simulated fills on the Stonks ledger"
            out.append(
                TradingModeView(
                    portfolio_id=p.id, name=p.name, trading=trading, broker=broker, detail=detail
                )
            )
        return out

    def totals(self, principal: Principal) -> PortfolioTotalsView:
        """Cash and value summed across every active portfolio (admins)."""
        require(principal, Permission.PORTFOLIO_TOTALS)
        with self._ctx.state() as state:
            books = state.sql(TOTALS_PORTFOLIOS_SQL)
            row = state.sql(
                """
                SELECT COALESCE(SUM(s.cash), 0) AS cash,
                       COALESCE(SUM(s.total_value), 0) AS total
                  FROM portfolios p
                  JOIN portfolio_snapshots s ON s.id = (
                        SELECT MAX(id) FROM portfolio_snapshots WHERE portfolio_id = p.id)
                 WHERE p.status = 'active' AND p.paper_of IS NULL
                """
            )[0]
        owners = {str(r["owner_id"]) for r in books}
        hidden = totals_suppressed(owners, principal.user_id)
        return PortfolioTotalsView(
            portfolios=len(books),
            owners=len(owners),
            cash=0.0 if hidden else float(row["cash"]),
            total_value=0.0 if hidden else float(row["total"]),
            suppressed=hidden,
        )

    def current(self, portfolio_id: str = DEFAULT_PORTFOLIO_ID) -> PortfolioView:
        """One portfolio's book (default: the default portfolio). Callers
        resolve ``portfolio_id`` through ``accounts.owned_portfolio`` first."""
        with self._ctx.state() as state:
            where, params = ledger_filter(state, "portfolio_snapshots", portfolio_id)
            rows = state.sql(
                f"SELECT * FROM portfolio_snapshots WHERE {where} ORDER BY id DESC LIMIT 1",
                params,
            )
        if not rows:
            cash = float(self._ctx.settings.production.initial_cash)
            return PortfolioView(
                taken_at=None,
                tick_id=None,
                cash=cash,
                positions=[],
                positions_value=0.0,
                total_value=cash,
                snapshot_total_value=None,
            )
        row = rows[0]
        holdings: dict[str, float] = json.loads(row["positions_json"])
        latest = self._latest_closes(list(holdings))
        currencies = self._currencies(list(holdings))
        costs = self._average_costs(portfolio_id)
        cash = float(row["cash"])

        positions: list[PositionView] = []
        for ticker in sorted(holdings):
            qty = float(holdings[ticker])
            price, price_date = latest.get(ticker, (None, None))
            positions.append(
                _position(ticker, qty, price, price_date, costs.get(ticker), currencies.get(ticker))
            )
        positions_value = sum(p.market_value or 0.0 for p in positions)
        total = cash + positions_value
        for p in positions:
            if p.market_value is not None and total:
                p.weight = p.market_value / total
        held_currencies = {currencies.get(t) for t in holdings}
        return PortfolioView(
            currency=_single(held_currencies) or DEFAULT_CURRENCY,
            cost_basis=sum(p.cost_basis or 0.0 for p in positions),
            unrealized_pnl=sum(p.unrealized_pnl or 0.0 for p in positions),
            taken_at=datetime.fromisoformat(row["taken_at"]),
            tick_id=row["tick_id"],
            cash=cash,
            positions=positions,
            positions_value=positions_value,
            total_value=total,
            snapshot_total_value=float(row["total_value"]),
        )

    def snapshots(
        self, *, limit: int, offset: int, portfolio_id: str = DEFAULT_PORTFOLIO_ID
    ) -> Page[SnapshotView]:
        with self._ctx.state() as state:
            where, params = ledger_filter(state, "portfolio_snapshots", portfolio_id)
            total = int(
                state.sql(f"SELECT COUNT(*) FROM portfolio_snapshots WHERE {where}", params)[0][0]
            )
            rows = state.sql(
                f"SELECT * FROM portfolio_snapshots WHERE {where}"
                " ORDER BY id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            )
        items = [
            SnapshotView(
                id=r["id"],
                tick_id=r["tick_id"],
                taken_at=datetime.fromisoformat(r["taken_at"]),
                cash=float(r["cash"]),
                positions=json.loads(r["positions_json"]),
                total_value=float(r["total_value"]),
            )
            for r in rows
        ]
        return Page[SnapshotView](items=items, total=total, limit=limit, offset=offset)

    def _average_costs(self, portfolio_id: str) -> dict[str, float]:
        with self._ctx.state() as state:
            where, params = ledger_filter(state, "fills", portfolio_id, alias="f")
            rows = state.sql(
                f"""
                SELECT f.ticker, f.quantity, f.price, f.fee, o.side
                  FROM fills f JOIN orders o ON o.client_id = f.order_client_id
                 WHERE {where}
                 ORDER BY f.filled_at, f.id
                """,
                params,
            )
        return average_costs(
            FillLot(
                ticker=r["ticker"],
                quantity=float(r["quantity"]) * (1 if r["side"] == "buy" else -1),
                price=float(r["price"]),
                fee=float(r["fee"] or 0.0),
            )
            for r in rows
        )

    def _currencies(self, tickers: list[str]) -> dict[str, str]:
        if not tickers:
            return {}
        with self._ctx.lake() as lake:
            df = lake.sql(
                "SELECT id, currency FROM instruments WHERE id = ANY(?) AND currency IS NOT NULL",
                [tickers],
            )
        return {r.id: str(r.currency).upper() for r in df.itertuples(index=False) if r.currency}

    def _latest_closes(self, tickers: list[str]) -> dict[str, tuple[float, date]]:
        if not tickers:
            return {}
        with self._ctx.lake() as lake:
            df = lake.sql(
                """
                SELECT ticker, max(date) AS date, arg_max(close, date) AS close
                  FROM prices
                 WHERE ticker = ANY(?)
                 GROUP BY ticker
                """,
                [tickers],
            )
        return {
            r.ticker: (float(r.close), r.date.date() if isinstance(r.date, datetime) else r.date)
            for r in df.itertuples(index=False)
        }


def _position(
    ticker: str,
    qty: float,
    price: float | None,
    price_date: date | None,
    avg_cost: float | None,
    currency: str | None,
) -> PositionView:
    cost_basis = None if avg_cost is None else avg_cost * qty
    pnl = pnl_pct = None
    if avg_cost is not None and price is not None:
        pnl = (price - avg_cost) * qty
        pnl_pct = pnl / abs(cost_basis) if cost_basis else None
    return PositionView(
        ticker=ticker,
        quantity=qty,
        price=price,
        price_date=price_date,
        market_value=None if price is None else qty * price,
        weight=None,
        currency=currency,
        avg_cost=avg_cost,
        cost_basis=cost_basis,
        unrealized_pnl=pnl,
        unrealized_pnl_pct=pnl_pct,
    )


def _single(values: set[str | None]) -> str | None:
    """The one value every element shares, else ``None``."""
    return next(iter(values)) if len(values) == 1 else None
