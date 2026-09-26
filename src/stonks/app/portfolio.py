"""PortfolioService — the book as of the latest tick, valued at the latest
stored closes, plus snapshot history.

Every read is of one portfolio the caller owns: :meth:`PortfolioService.resolve`
turns a principal and an optional ``portfolio_id`` into that id (404 for a
portfolio that isn't yours, admins included). Admins get
:meth:`PortfolioService.totals`, sums across every book, never holdings."""

from __future__ import annotations

import json
from datetime import date, datetime

from pydantic import BaseModel, Field

from stonks.accounts import NotFound, owned_portfolio
from stonks.accounts.models import DEFAULT_PORTFOLIO_ID
from stonks.app.context import AppContext
from stonks.app.cost_basis import FillLot, average_costs
from stonks.app.errors import NotFoundError
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

    def totals(self, principal: Principal) -> PortfolioTotalsView:
        """Cash and value summed across every active portfolio (admins)."""
        require(principal, Permission.PORTFOLIO_TOTALS)
        with self._ctx.state() as state:
            row = state.sql(
                """
                SELECT COUNT(*) AS n, COUNT(DISTINCT p.owner_id) AS owners,
                       COALESCE(SUM(s.cash), 0) AS cash,
                       COALESCE(SUM(s.total_value), 0) AS total
                  FROM portfolios p
                  JOIN portfolio_snapshots s ON s.id = (
                        SELECT MAX(id) FROM portfolio_snapshots WHERE portfolio_id = p.id)
                 WHERE p.status = 'active'
                """
            )[0]
        return PortfolioTotalsView(
            portfolios=int(row["n"]),
            owners=int(row["owners"]),
            cash=float(row["cash"]),
            total_value=float(row["total"]),
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
